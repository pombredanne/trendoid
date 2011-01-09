#!/usr/bin/env python

from django.utils import simplejson

from google.appengine.api import users
from google.appengine.ext import db, webapp
from google.appengine.ext.webapp import util


class Project(db.Model):
    slug = db.StringProperty(required=True)
    title = db.StringProperty(required=True)
    api_key = db.StringProperty(required=True, verbose_name="API key value used to allow writes")

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


class ProjectHandler(webapp.RequestHandler):
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


class DataCollector(webapp.RequestHandler):
    """
    Dirt-simple data receiver
    """

    def post(self, project_name=None):
        """
        POST handler which receives two mandatory values (project and api_key)
        and one or more
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


def main():
    application = webapp.WSGIApplication([
        ('^/project$', ProjectHandler),
        ('^/data$', DataCollector),
        ('^/project/(?P<project_name>\w+)/data$', DataCollector),
    ], debug=True)
    util.run_wsgi_app(application)

if __name__ == '__main__':
    main()
