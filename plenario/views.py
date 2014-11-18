from flask import make_response, request, redirect, url_for, render_template, current_app, g, \
    Blueprint, flash, session as flask_session
from plenario.models import MasterTable, MetaTable, User
from plenario.database import session, Base, app_engine as engine
from plenario.utils.helpers import get_socrata_data_info, iter_column, send_mail, slugify
from plenario.tasks import update_dataset as update_dataset_task, \
    delete_dataset as delete_dataset_task, add_dataset as add_dataset_task
from flask_login import login_required
from datetime import datetime, timedelta
from urlparse import urlparse
import requests
from flask_wtf import Form
from wtforms import TextField, PasswordField, DateField, SelectField
from wtforms.validators import DataRequired, Email
from dateutil import parser
import json
import re
from cStringIO import StringIO
from csvkit.unicsv import UnicodeCSVReader
from sqlalchemy import Table
from plenario.settings import CACHE_CONFIG
import string
import sqlalchemy
from hashlib import md5

views = Blueprint('views', __name__)

@views.route('/')
def index():
    return render_template('index.html')

@views.route('/explore')
def explore_view():
    return render_template('explore.html')

@views.route('/api-docs')
def api_docs_view():
    return render_template('api-docs.html')

@views.route('/about')
def about_view():
    return render_template('about.html')

@views.route('/examples')
def examples_view():
    return render_template('examples.html')

@views.route('/maintenance')
def maintenance():
    return render_template('maintenance.html'), 503

# Given a URL, this function returns a tuple (dataset_info, errors, socrata_source)
def get_context_for_new_dataset(url):
    dataset_info = {}
    errors = []
    socrata_source = False
    if url:
        url = url.strip(' \t\n\r') # strip whitespace, tabs, etc
        four_by_four = re.findall(r'/([a-z0-9]{4}-[a-z0-9]{4})', url)
        errors = True
        if four_by_four:
            parsed = urlparse(url)
            host = 'https://%s' % parsed.netloc
            path = 'api/views'
            view_url = '%s/%s/%s' % (host, path, four_by_four[-1])

            dataset_info, errors, status_code = get_socrata_data_info(view_url)
            if not errors:
                socrata_source = True
                dataset_info['submitted_url'] = url
        if errors:
            errors = []
            try:
                r = requests.get(url, stream=True)
                status_code = r.status_code
            except requests.exceptions.InvalidURL:
                errors.append('Invalid URL')
            except requests.exceptions.ConnectionError:
                errors.append('URL can not be reached')
            if status_code != 200:
                errors.append('URL returns a %s status code' % status_code)
            if not errors:
                dataset_info['submitted_url'] = url
                dataset_info['name'] = urlparse(url).path.split('/')[-1]
                inp = StringIO()
                line_no = 0
                lines = []
                for line in r.iter_lines():
                    try:
                        inp.write(line + '\n')
                        line_no += 1
                        if line_no > 1000:
                            raise StopIteration
                    except StopIteration:
                        break
                inp.seek(0)
                reader = UnicodeCSVReader(inp)
                header = reader.next()
                col_types = []
                inp.seek(0)
                for col in range(len(header)):
                    col_types.append(iter_column(col, inp))
                dataset_info['columns'] = []
                for idx, col in enumerate(col_types):
                    d = {
                        'human_name': header[idx],
                        'data_type': col.__visit_name__.lower()
                    }
                    dataset_info['columns'].append(d)
    else:
        errors.append('Need a URL')
    #print "get_context_for_new_dataset(): returning ", dataset_info, errors, socrata_source
    return (dataset_info, errors, socrata_source)

def approve_dataset(source_url_hash):
    # get the MetaTable row and change the approved_status and bounce back to view-datasets.

    meta = session.query(MetaTable).get(source_url_hash)

    json_data_types = None
    if ((not meta.is_socrata_source) and meta.contributed_data_types):
        json_data_types = json.loads(meta.contributed_data_types)
        
    add_dataset_task.delay(source_url_hash, data_types=json_data_types)
    
    upd = { 'approved_status': 'true' }

    meta.approved_status = 'true'
    session.commit()

    # Email the user who submitted that their dataset has been approved.
    # email the response to somebody

    msg_body = """Hello %s,\r\n
\r\n
Your dataset has been approved and added to Plenar.io:\r\n
\r\n
%s\r\n
\r\n
It should appear on http://plenar.io within 24 hours.\r\n
\r\n
Thank you!\r\n
The Plenario Team\r\n
http://plenar.io""" % (meta.contributor_name, meta.human_name)

    send_mail(subject="Your dataset has been added to Plenar.io", 
        recipient=meta.contributor_email, body=msg_body)

# /contribute is similar to /admin/add-dataset, but sends an email instead of actually adding
@views.route('/contribute', methods=['GET','POST'])
def contrib_view():
    dataset_info = {}
    errors = []
    socrata_source = False

    url = ""
    dataset_id = None
    md = None

    if request.args.get('dataset_url'):

        url = request.args.get('dataset_url')
        (dataset_info, errors, socrata_source) = get_context_for_new_dataset(url)

        # check if dataset with the same URL has already been loaded
        dataset_id = md5(url).hexdigest()
        md = session.query(MetaTable).get(dataset_id)
        if md:
            errors.append("A dataset with that URL has already been loaded: '%s'" % md.human_name)

    if request.method == 'POST' and not md:

        data_types = []
        business_key = None
        observed_date = None
        latitude = None
        longitude = None 
        location = None
        for k, v in request.form.iteritems():
            if k.startswith('data_type_'):
                key = k.replace("data_type_", "")
                data_types.append({"field_name": key, "data_type": v})

            if k.startswith('key_type_'):
                key = k.replace("key_type_", "")
                if (v == "business_key"): business_key = key
                if (v == "observed_date"): observed_date = key
                if (v == "latitude"): latitude = key
                if (v == "longitude"): longitude = key
                if (v == "location"): location = key

        d = {
            'dataset_name': slugify(request.form.get('dataset_name'), delim=u'_')[:50],
            'human_name': request.form.get('dataset_name'),
            'attribution': request.form.get('dataset_attribution'),
            'description': request.form.get('dataset_description'),
            'source_url': url,
            'source_url_hash': dataset_id,
            'update_freq': request.form.get('update_frequency'),
            'business_key': business_key,
            'observed_date': observed_date,
            'latitude': latitude,
            'longitude': longitude,
            'location': location,
            'contributor_name': request.form.get('contributor_name'),
            'contributor_organization': request.form.get('contributor_organization'),
            'contributor_email': request.form.get('contributor_email'),
            'contributed_data_types': json.dumps(data_types),
            'approved_status': 'false',
            'is_socrata_source': socrata_source
        }

        print d

        # add this to meta_master
        md = MetaTable(**d)
        session.add(md)
        session.commit()

        # email a confirmation to the submitter
        msg_body = """Hello %s,\r\n\r\n
We received your recent dataset submission to Plenar.io:\r\n\r\n%s\r\n\r\n
After we review it, we'll notify you when your data is loaded and available.\r\n\r\n
Thank you!\r\nThe Plenario Team\r\nhttp://plenar.io""" % (d['contributor_name'], md.human_name)

        send_mail(subject="Your dataset has been submitted to Plenar.io", 
            recipient=d['contributor_email'], body=msg_body)

        return redirect(url_for('views.contrib_thankyou'))

    context = {'dataset_info': dataset_info, 'form': request.form, 'errors': errors, 'socrata_source': socrata_source}
    return render_template('contribute.html', **context)

@views.route('/contribute-thankyou')
def contrib_thankyou():
    context = {}
    return render_template('contribute_thankyou.html', **context)


@views.route('/admin/add-dataset', methods=['GET', 'POST'])
@login_required
def add_dataset():
    dataset_info = {}
    errors = []
    socrata_source = False
    if request.method == 'POST':
        url = request.form.get('dataset_url')
        (dataset_info, errors, socrata_source) = get_context_for_new_dataset(url)
        user = session.query(User).get(flask_session['user_id'])
        dataset_info['contributor_name'] = user.name
        dataset_info['contributor_organization'] = 'Plenario Admin'
        dataset_info['contributor_email'] = user.email
    context = {'dataset_info': dataset_info, 'errors': errors, 'socrata_source': socrata_source}
    return render_template('admin/add-dataset.html', **context)

@views.route('/admin/view-datasets')
@login_required
def view_datasets():
    datasets_pending = session.query(MetaTable).filter(MetaTable.approved_status != 'true').all()
    datasets = session.query(MetaTable).filter(MetaTable.approved_status == 'true').all()
    return render_template('admin/view-datasets.html', datasets_pending=datasets_pending, datasets=datasets)

class EditDatasetForm(Form):
    """ 
    Form to edit meta_master information for a dataset
    """
    human_name = TextField('human_name', validators=[DataRequired()])
    description = TextField('description', validators=[DataRequired()])
    attribution = TextField('attribution', validators=[DataRequired()])
    #obs_from = DateField('obs_from', validators=[DataRequired(message="Start of date range must be a valid date")])
    #obs_to = DateField('obs_to', validators=[DataRequired(message="End of date range must be a valid date")])
    update_freq = SelectField('update_freq', 
                              choices=[('daily', 'Daily'),
                                       ('weekly', 'Weekly'),
                                       ('monthly', 'Monthly'),
                                       ('yearly', 'Yearly')], 
                              validators=[DataRequired()])
    business_key = TextField('business_key', validators=[DataRequired()])
    observed_date = TextField('observed_date', validators=[DataRequired()])
    latitude = TextField('latitude')
    longitude = TextField('longitude')
    location = TextField('location')

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        
        valid = True
        
        if not self.location.data and not self.latitude.data and not self.longitude.data:
            valid = False
            self.location.errors.append('You must either provide a Latitude and Longitude field name or a Location field name')
        
        if self.longitude.data and not self.latitude.data:
            valid = False
            self.latitude.errors.append('You must provide both a Latitude field name and a Longitude field name')
        
        if self.latitude.data and not self.longitude.data:
            valid = False
            self.longitude.errors.append('You must provide both a Latitude field name and a Longitude field name')

        return valid

@views.route('/admin/approve-dataset/<source_url_hash>', methods=['GET', 'POST'])
@login_required
def approve_dataset_view(source_url_hash):
    
    approve_dataset(source_url_hash)
    
    return redirect(url_for('views.view_datasets'))


@views.route('/admin/edit-dataset/<source_url_hash>', methods=['GET', 'POST'])
@login_required
def edit_dataset(source_url_hash):
    form = EditDatasetForm()
    meta = session.query(MetaTable).get(source_url_hash)

    fieldnames = None
    if (meta.approved_status == 'true'):
        try:
            table = Table('dat_%s' % meta.dataset_name, Base.metadata,
                          autoload=True, autoload_with=engine)
            fieldnames = table.columns.keys()
        except sqlalchemy.exc.NoSuchTableError, e:
            # dataset has been approved, but perhaps still processing.
            pass

    if (not fieldnames):
        fieldnames = []
        if meta.contributed_data_types:
            fieldnames = [f['field_name'] for f in json.loads(meta.contributed_data_types)]
    if form.validate_on_submit():
        upd = {
            'human_name': form.human_name.data,
            'description': form.description.data,
            'attribution': form.attribution.data,
            'update_freq': form.update_freq.data,
            'business_key': form.business_key.data,
            'latitude': form.latitude.data,
            'longitude': form.longitude.data,
            'location': form.location.data,
            'observed_date': form.observed_date.data,
        }
        session.query(MetaTable)\
            .filter(MetaTable.source_url_hash == meta.source_url_hash)\
            .update(upd)
        session.commit()

        
        if (meta.approved_status != 'true'):
            approve_dataset(source_url_hash)
        
        flash('%s updated successfully!' % meta.human_name, 'success')
        return redirect(url_for('views.view_datasets'))
    else:
        pass
    context = {
        'form': form,
        'meta': meta,
        'fieldnames': fieldnames,
    }
    return render_template('admin/edit-dataset.html', **context)

@views.route('/admin/delete-dataset/<source_url_hash>')
@login_required
def delete_dataset(source_url_hash):
    result = delete_dataset_task.delay(source_url_hash)
    return make_response(json.dumps({'status': 'success', 'task_id': result.id}))

@views.route('/update-dataset/<source_url_hash>')
def update_dataset(source_url_hash):
    result = update_dataset_task.delay(source_url_hash)
    return make_response(json.dumps({'status': 'success', 'task_id': result.id}))

@views.route('/check-update/<task_id>')
def check_update(task_id):
    result = update_dataset_task.AsyncResult(task_id)
    if result.ready():
        r = {'status': 'ready'}
    else:
        r = {'status': 'pending'}
    resp = make_response(json.dumps(r))
    resp.headers['Content-Type'] = 'application/json'
    return resp

@views.route('/terms')
def terms_view():
    return render_template('terms.html')
