#!/usr/bin/env python

import os
import time

from django.utils import simplejson

from google.appengine.api import users
from google.appengine.ext import db, webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app


APP_ROOT = os.path.dirname(__file__)


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

    def put(self, *args, **kwargs):
        super(DataPoint, self).put(*args, **kwargs)

        # Now that we've been saved we'll update the project's list of field
        # names so retrieval can be fast

        # To avoid repeated lookups:
        project = self.project

        new_fields = [k for k in self.dynamic_properties() if k not in project.field_names]
        if new_fields:
            project.field_names = list(set(project.field_names + new_fields))
            project.put()


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
            data = [("%sZ" % i.timestamp.isoformat(),
                    getattr(i, field_name, None)) for i in project.data_points.order("timestamp")]
            self.response.out.write(simplejson.dumps(data))

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

        DataPoint(project=project, remote_addr=self.request.remote_addr, **data).put()

        self.response.clear()
        self.response.set_status(201)


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
    ], debug=True)

    run_wsgi_app(application)

if __name__ == '__main__':
    main()
