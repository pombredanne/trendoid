#!/usr/bin/env python

from datetime import date, datetime, timedelta
import os
import time
import re

from django.utils import simplejson

from google.appengine.api import taskqueue, users
from google.appengine.ext import db, webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app


APP_ROOT = os.path.dirname(__file__)

DATE_RE = re.compile('^\d{4}-\d{2}-\d{2}$')

class Project(db.Model):
    slug = db.StringProperty(required=True)
    title = db.StringProperty(required=True)
    api_key = db.StringProperty(required=True, verbose_name="API key value used to allow writes")

    field_names = db.StringListProperty()

    @classmethod
    def create(cls, slug=None, title=None, api_key=None):
        key_name = "project/%s" % slug

        if Project.get_by_key_name(key_name) is not None:
            raise ValueError("project %s already exists" % slug)

        prj = Project(key_name=key_name, slug=slug, title=title, api_key=api_key)
        prj.put()

        return prj


class DataPoint(db.Expando):
    project = db.ReferenceProperty(Project, collection_name="data_points")
    timestamp = db.DateTimeProperty(auto_now_add=True)
    remote_addr = db.StringProperty(required=True)


class DataAggregate(db.Model):
    """
    Simple class used to aggregate values for a given range

    key_name must be constructed to scope your data - for example:
        '%(project)s:%(field)s:%(date)s'
    """

    #: Used to retrieve data for a specific field:
    field_name = db.StringProperty()

    #: ISO 8601 value used solely for querying:
    date = db.StringProperty()

    values = db.ListProperty(float)

    min = db.FloatProperty()
    max = db.FloatProperty()
    average = db.FloatProperty()
    median = db.FloatProperty()

    def put(self, *args, **kwargs):
        if self.values:
            self.min = min(self.values)
            self.max = max(self.values)
            self.average = sum(self.values) / len(self.values)

            sorted_values = sorted(self.values)
            self.median = sorted_values[len(sorted_values) / 2]
        else:
            self.min = self.max = self.average = self.median = 0.0

        super(DataAggregate, self).put(*args, **kwargs)

    @classmethod
    def get_or_create(cls, project_slug, field, date_iso):
        key_name = "aggregate/%s:%s:%s" % (project_slug, field, date_iso)
        agg = cls.get_by_key_name(key_name)
        if agg is None:
            agg = DataAggregate(key_name=key_name)
            agg.date = date_iso

        agg.field_name = '%s:%s' % (project_slug, field)

        return agg


class ProjectHandler(webapp.RequestHandler):
    def get(self, project_name=None):
        user = users.get_current_user()

        if project_name is None:
            context = {"projects": Project.all(), 'user': user,
                        'is_admin': users.is_current_user_admin()}

            if user:
                context['logout_url'] = users.create_logout_url("/")
            else:
                context['login_url'] = users.create_login_url("/")

            resp = render_template('templates/project_list.html', context)
        else:
            project = Project.get_by_key_name("project/%s" % project_name)

            if project is None:
                return self.error(404)

            context = {"project": project}
            resp = render_template('templates/project_detail.html', context)

        self.response.out.write(resp)

    def post(self):
        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(self.request.uri))
            return

        if not users.is_current_user_admin():
            self.error(401)
            return

        prj_args = {
            "slug": self.request.get("slug"),
            "title": self.request.get("title"),
            "api_key": self.request.get("api_key"),
        }

        if not all(prj_args.values()):
            self.error(400)
            return

        try:
            Project.create(**prj_args)
        except ValueError:
            self.error(400)
            return

        self.response.set_status(201)


class ProjectDataHandler(webapp.RequestHandler):
    """
    Dirt-simple data handler with minimal, JSON/POST-only interfaces
    """

    def get(self, project_name=None, field_name=None):
        project = Project.get_by_key_name("project/%s" % project_name)

        if project is None:
            self.error(400)
            return

        if field_name is None:
            self.response.out.write(simplejson.dumps({"fields": project.field_names}))
        else:
            # We'll default to data in the last week:
            start_date = self.request.get("start_date", None) or (date.today() - timedelta(7)).isoformat()
            end_date = self.request.get("end_date", None) or date.today().isoformat()

            if not DATE_RE.match(start_date) or not DATE_RE.match(end_date):
                return self.error(400)

            data = []
            aggregates = DataAggregate.gql("WHERE field_name = :field_name AND date >= :start_date AND date <= :end_date",
                                            field_name="%s:%s" % (project.slug, field_name),
                                            start_date=start_date, end_date=end_date)
            for agg in aggregates:
                data.append((agg.date, (agg.min, agg.median, agg.max)))

            self.response.out.write(simplejson.dumps(sorted(data)))

        self.response.headers['Content-Type'] = "application/json"
        self.response.headers['Cache-Control'] = "public; max-age=300"

    def post(self, project_name=None):
        """
        POST handler which receives two mandatory values (project and api_key)
        and one or more form parameters with arbitrary keys and float values
        """

        # For convenience we allow the project to be provided using a
        # subdomain or as a form value:
        if project_name is None:
            if 'project' in self.request.POST:
                project_name = self.request.get("project")
            else:
                project_name = self.request.headers['host'].split(".")[0]

        project = Project.get_by_key_name("project/%s" % project_name)

        if project is None:
            self.error(400)
            return

        api_key = self.request.get("api_key", None)

        if api_key != project.api_key:
            self.error(403)
            return

        data = {}

        for k in self.request.POST:
            if k in ('project', 'api_key'):
                continue

            try:
                data[str(k)] = float(self.request.POST[k])
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.error(400)

        if not data:
            self.error(400)
            return

        DataPoint(parent=project, project=project, remote_addr=self.request.remote_addr, **data).put()

        self.response.clear()
        self.response.set_status(201)

        missing_fields = [k for k in data if k not in project.field_names]
        if missing_fields:
            project.field_names.extend(missing_fields)
            project.put()

        taskqueue.add(url='/aggregates/update', queue_name="aggregates",
                        params={'project': project.slug,
                                'date': date.today().isoformat()})

class AggregationHandler(webapp.RequestHandler):
    def post(self):
        """Updates aggregates for all data points"""

        start_date = self.request.get("date", None)
        if start_date is None:
            start_date = (date.today() - timedelta(days=1)).isoformat()

        if not DATE_RE.match(start_date):
            return self.error(400)

        try:
            start_date = date(*map(int, start_date.split("-")))
        except ValueError:
            return self.error(400)

        start_dt = datetime.fromordinal(start_date.toordinal())
        end_dt = start_dt.replace(hour=23, minute=59, second=59)

        project = self.request.get("project", None)
        if project:
            project = Project.get_by_key_name("project/%s" % project)
            if not project:
                return self.error(400)
            projects = [project]
        else:
            projects = Project.all()

        for project in projects:
            aggregates = {}
            for field in project.field_names:
                agg = DataAggregate.get_or_create(project.slug, field, start_date.isoformat())
                agg.values = [] # Clear out anything which already existed
                aggregates[field] = agg

            data_points = project.data_points.filter("timestamp >=", start_dt).filter('timestamp <=', end_dt)

            for point in data_points:
                for field in point.dynamic_properties():
                    aggregates[field].values.append(getattr(point, field))

            for agg in aggregates.values():
                if len(agg.values) == 0:
                    agg.delete()
                else:
                    agg.put()

def render_template(template_name, extra_context=None):
    context = {
        "STATIC_VERSION": os.environ.get("CURRENT_VERSION_ID", None) or time.time()
    }

    if extra_context:
        context.update(extra_context)

    template_file = os.path.join(APP_ROOT, template_name)

    return template.render(template_file, context)


def main():
    application = webapp.WSGIApplication([
        ('^/$', ProjectHandler),
        ('^/(?P<project_name>\w+)/$', ProjectHandler),
        ('^/(?P<project_name>\w+)/data/$', ProjectDataHandler),
        ('^/(?P<project_name>\w+)/data/(?P<field_name>\w+)/$', ProjectDataHandler),
        ('^/aggregates/update$', AggregationHandler),
    ], debug=True)

    run_wsgi_app(application)

if __name__ == '__main__':
    main()
