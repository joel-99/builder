"""
Marshalls a collection of project information together in to a dictionary called the `context`.

When an instance of a project is launched on AWS, we need to tweak things a bit
with no manual steps in some cases, or as few as possible in other cases.

Case 1: New, standardized environment
We launch journal--ci, a testing instance for the journal project within the `ci` environment.

Case 2: Ad-hoc instances
We launch journal--testsomething, a testing instance we will use to check something works as expected.

Case 3: Stack updates
We want to add an external volume to an EC2 instance to increase available space, so we partially update the CloudFormation template to create it.

"""
import os, json, copy
import re
from collections import OrderedDict, namedtuple
import netaddr
from slugify import slugify
from . import utils, cloudformation, terraform, core, project, context_handler
from .utils import ensure, lmap, mkdir_p
from .config import STACK_DIR, TERRAFORM_DIR

import logging

LOG = logging.getLogger(__name__)

# TODO: this function needs some TLC - it's getting fat.
def build_context(pname, **more_context): # pylint: disable=too-many-locals
    """wrangles parameters into a dictionary (context) that can be given to
    whatever renders the final template"""

    supported_projects = project.project_list()
    ensure(pname in supported_projects, "Unknown project %r. Known projects: %s" % (pname, supported_projects))

    # this is the context data from the currently existing template (if any)
    # by re-using current values we can avoid making unnecessary changes when
    # regenerating templates (like random passwords)
    existing_context = more_context.pop('existing_context', {})

    # order is important. always use the alt-config in more_context (explicit) also when regenerating
    alt_config = more_context.get('alt-config')

    project_data = project.project_data(pname)
    if alt_config and project_data.get('aws-alt', {}).get(alt_config):
        project_data = project.set_project_alt(project_data, 'aws', alt_config)

    defaults = {
        'project_name': pname,
        'project': project_data,

        'author': os.environ.get("LOGNAME") or 'unknown',
        'date_rendered': utils.ymd(), # TODO: if this value is used at all, more precision might be nice

        # a stackname looks like: <pname>--<instance_id>[--<cluster-id>]
        'stackname': None, # must be provided by whatever is calling this
        'instance_id': None, # derived from the stackname
        'cluster_id': None, # derived from the stackname

        'alt-config': None,

        'branch': project_data.get('default-branch'),
        'revision': None, # may be used in future to checkout a specific revision of project

        # TODO: shift these rds_ values under the 'rds' key
        'rds_dbname': None, # generated from the instance_id when present
        'rds_username': None, # could possibly live in the project data, but really no need.
        'rds_password': None,
        'rds_instance_id': None,
        'rds': {},

        'ec2': False,
        's3': {},
        'elb': False,
        'sns': [],
        'sqs': {},
        'ext': False,
        'cloudfront': False,
        'elasticache': False,
    }

    context = copy.deepcopy(defaults)
    context.update(more_context)

    # proceed with wrangling

    # TODO: don't like this. if a stackname is required, make it a parameter.
    # stackname used to be derived inside this func from pname + id + cluster number
    ensure(context['stackname'], "a stackname wasn't provided.")
    stackname = context['stackname']

    # stackname data
    bits = core.parse_stackname(stackname, all_bits=True, idx=True)
    ensure(bits['project_name'] == pname,
           "the project name %r derived from the given `stackname` %r doesn't match" % (bits['project_name'], pname))
    context.update(bits)

    # hostname data
    context.update(core.hostname_struct(stackname))

    # rds
    context.update(build_context_rds(context, existing_context))

    if 'ext' in context['project']['aws']:
        context['ext'] = context['project']['aws']['ext']

    # ec2
    # TODO: this is a problem. using the default 'True' preserves the behaviour of
    # when 'ec2: True' meant, 'use defaults with nothing changed'
    # but now I need to store master ip info there.
    #context['ec2'] = context['project']['aws'].get('ec2', True)
    context['ec2'] = context['project']['aws']['ec2']
    if context['ec2'] == True:
        context['ec2'] = {}
        context['project']['aws']['ec2'] = {}
        LOG.warn("stack needs it's context refreshed: %s", stackname)
        # we can now assume these will always be dicts

    if isinstance(context['ec2'], dict): # the other case is aws.ec2 == False
        context['ec2']['type'] = context['project']['aws']['type'] # TODO: shift aws.type to aws.ec2.type in project file
        context = set_master_address(context)

    build_context_elb(context)

    def _parameterize(string):
        return string.format(instance=context['instance_id'])

    for topic_template_name in context['project']['aws']['sns']:
        topic_name = _parameterize(topic_template_name)
        context['sns'].append(topic_name)

    for queue_template_name in context['project']['aws']['sqs']:
        queue_name = _parameterize(queue_template_name)
        queue_configuration = context['project']['aws']['sqs'][queue_template_name]
        subscriptions = lmap(_parameterize, queue_configuration.get('subscriptions', []))
        context['sqs'][queue_name] = subscriptions

    # future: build what is necessary for buildercore.bootstrap.setup_s3()
    default_bucket_configuration = {
        'sqs-notifications': {},
        'deletion-policy': 'delete',
        'website-configuration': None,
        'cors': None,
        'public': False,
    }
    for bucket_template_name in context['project']['aws']['s3']:
        bucket_name = _parameterize(bucket_template_name)
        configuration = context['project']['aws']['s3'][bucket_template_name]
        context['s3'][bucket_name] = default_bucket_configuration.copy()
        context['s3'][bucket_name].update(configuration if configuration else {})

    build_context_cloudfront(context, parameterize=_parameterize)
    build_context_fastly(context, parameterize=_parameterize)
    build_context_subdomains(context)
    build_context_elasticache(context)

    return context

def set_master_address(data, master_ip=None):
    "can update both context and buildvars data"
    master_ip = master_ip or data['ec2'].get('master_ip')  # or data['project']['aws']['ec2']['master_ip']
    ensure(master_ip, "a master-ip was neither explicitly given nor found in the data provided")
    data['ec2']['master_ip'] = master_ip
    if 'aws' in data['project']:
        # context (rather than buildvars)
        data['project']['aws']['ec2']['master_ip'] = master_ip
        if data['ec2'].get('masterless'):
            # this is a masterless instance, delete key
            del data['project']['aws']['ec2']['master_ip']
    return data

def build_context_rds(context, existing_context):
    if 'rds' not in context['project']['aws']:
        return {}
    stackname = context['stackname']

    # deletion policy
    deletion_policy = utils.lookup(context, 'project.aws.rds.deletion-policy', 'Snapshot')

    # used to give mysql a range of valid ip addresses to connect from
    subnet_cidr = netaddr.IPNetwork(context['project']['aws']['subnet-cidr'])
    net = subnet_cidr.network
    mask = subnet_cidr.netmask
    networkmask = "%s/%s" % (net, mask) # ll: 10.0.2.0/255.255.255.0

    # pull password from existing context, if it exists
    generated_password = utils.random_alphanumeric(length=32)
    rds_password = existing_context.get('rds_password', generated_password)

    return {
        'netmask': networkmask,
        'rds_username': 'root',
        'rds_password': rds_password,
        # alpha-numeric only
        # TODO: investigate possibility of ambiguous RDS naming here
        'rds_dbname': context.get('rds_dbname') or slugify(stackname, separator=""), # *must* use 'or' here
        'rds_instance_id': slugify(stackname), # *completely* different to database name
        'rds_params': context['project']['aws']['rds'].get('params', []),

        'rds': {
            'deletion-policy': deletion_policy
        }
    }


def build_context_elb(context):
    if 'elb' in context['project']['aws']:
        if isinstance(context['project']['aws']['elb'], dict):
            context['elb'] = context['project']['aws']['elb']
        else:
            context['elb'] = {}
        context['elb'].update({
            'subnets': [
                context['project']['aws']['subnet-id'],
                context['project']['aws']['redundant-subnet-id']
            ],
        })

def build_context_cloudfront(context, parameterize):
    def build_subdomain(x):
        return complete_domain(parameterize(x), context['domain'])
    if 'cloudfront' in context['project']['aws']:
        errors = None
        if context['project']['aws']['cloudfront']['errors']:
            errors = {
                'domain': parameterize(context['project']['aws']['cloudfront']['errors']['domain']),
                'pattern': context['project']['aws']['cloudfront']['errors']['pattern'],
                'codes': context['project']['aws']['cloudfront']['errors']['codes'],
                'protocol': context['project']['aws']['cloudfront']['errors']['protocol'],
            }
        context['cloudfront'] = {
            'subdomains': [build_subdomain(x) for x in context['project']['aws']['cloudfront']['subdomains']],
            'subdomains-without-dns': [build_subdomain(x) for x in context['project']['aws']['cloudfront']['subdomains-without-dns']],
            'certificate_id': context['project']['aws']['cloudfront']['certificate_id'],
            'cookies': context['project']['aws']['cloudfront']['cookies'],
            'compress': context['project']['aws']['cloudfront']['compress'],
            'headers': context['project']['aws']['cloudfront']['headers'],
            'default-ttl': context['project']['aws']['cloudfront']['default-ttl'],
            'errors': errors,
            'logging': context['project']['aws']['cloudfront'].get('logging', False),
            'origins': OrderedDict([
                (o_id, {
                    'hostname': parameterize(o['hostname']),
                    'pattern': o.get('pattern'),
                    'headers': o.get('headers', []),
                    'cookies': o.get('cookies', []),
                })
                for o_id, o in context['project']['aws']['cloudfront']['origins'].items()
            ]),
        }
    else:
        context['cloudfront'] = False

def build_context_fastly(context, parameterize):
    def build_subdomain(x):
        return complete_domain(parameterize(x), context['domain'])
    if 'fastly' in context['project']['aws']:
        context['fastly'] = {
            'subdomains': [build_subdomain(x) for x in context['project']['aws']['fastly']['subdomains']],
            'subdomains-without-dns': [build_subdomain(x) for x in context['project']['aws']['fastly']['subdomains-without-dns']],
            'dns': context['project']['aws']['fastly']['dns'],
        }
    else:
        context['fastly'] = False

def complete_domain(host, default_main):
    is_main = host == ''
    is_complete = host.count(".") > 0
    if is_main:
        return default_main
    elif is_complete:
        return host
    return host + '.' + default_main # something + '.' + elifesciences.org

def build_context_subdomains(context):
    context['subdomains'] = [complete_domain(s, context['project']['domain']) for s in context['project']['aws'].get('subdomains', [])]

def build_context_elasticache(context):
    if 'elasticache' in context['project']['aws']:
        context['elasticache'] = context['project']['aws']['elasticache']

def choose_alt_config(stackname):
    """returns the name of the alt-config you think the user would want, based on given stackname"""
    pname, instance_id = core.parse_stackname(stackname)
    pdata = project.project_data(pname)
    if instance_id in project.project_alt_config_names(pdata):
        # instance_id exactly matches an alternative config. use that.
        return instance_id

#
#
#

# TODO: move to cloudformation.py
def write_cloudformation_template(stackname, contents):
    "writes a json version of the python cloudformation template to the stacks directory"
    output_fname = os.path.join(STACK_DIR, stackname + ".json")
    open(output_fname, 'w').write(contents)
    return output_fname

# TODO: move to terraform.py
def write_terraform_template(stackname, contents):
    "optionally, store a terraform configuration file for the stack"
    # if the template isn't empty ...?
    if json.loads(contents):
        output_dir = os.path.join(TERRAFORM_DIR, stackname)
        mkdir_p(output_dir)
        output_fname = os.path.join(output_dir, "generated.tf")
        open(output_fname, 'w').write(contents)
        return output_fname

# TODO: prefer this single dispatch function for handling creation of template files
def write_template(stackname, contents):
    "writes any provider templates and returns a list of paths to templates"
    # cfn = cloudformation.write_template(stackname, contents)
    # tfm = terraform.write_template(stackname, contents)
    # return [cfn, tfm]
    pass

# TODO: perhaps add terraform support?
def read_template(stackname):
    "returns the contents of a cloudformation template as a python data structure"
    output_fname = os.path.join(STACK_DIR, stackname + ".json")
    return json.load(open(output_fname, 'r'))

#
#
#

def more_validation(json_template_str):
    "local cloudformation template checks. complements the validation AWS does"
    try:
        data = json.loads(json_template_str)
        # case: when "DBInstanceIdentifier" == "lax--temp2"
        # The parameter Filter: db-instance-id is not a valid identifier. Identifiers must begin with a letter;
        # must contain only ASCII letters, digits, and hyphens; and must not end with a hyphen or contain two consecutive hyphens.
        dbid = utils.lookup(data, 'Resources.AttachedDB.Properties.DBInstanceIdentifier', False)
        if dbid:
            ensure('--' not in dbid, "database instance identifier contains a double hyphen: %r" % dbid)

        return True
    except BaseException:
        LOG.exception("uncaught error attempting to validate cloudformation template")
        raise

#
#
#

def validate_project(pname, **extra):
    """validates all of project's possible cloudformation templates.
    only called during testing"""
    LOG.info('validating %s', pname)
    template = quick_render(pname)
    pdata = project.project_data(pname)
    altconfig = None
    cloudformation.validate_template(pname, template)
    more_validation(template)
    # validate all alternative configurations
    for altconfig in pdata.get('aws-alt', {}).keys():
        LOG.info('validating %s, %s', pname, altconfig)
        extra = {
            'alt-config': altconfig
        }
        template = quick_render(pname, **extra)
        cloudformation.validate_template(pname, template)

#
# create new template
#

def quick_render(project_name, **more_context):
    """generates a representative Cloudformation template for given project with dummy values
    only called during testing"""
    # set a dummy instance id if one hasn't been set.
    more_context['stackname'] = more_context.get('stackname', core.mk_stackname(project_name, 'dummy'))
    context = build_context(project_name, **more_context)
    return cloudformation.render_template(context)

def generate_stack(pname, **more_context):
    """given a project name and any context overrides, generates a Cloudformation
    stack file, writes it to file and returns a pair of (context, stackfilename)"""
    context = build_context(pname, **more_context)
    cloudformation_template = cloudformation.render_template(context)
    terraform_template = terraform.render(context)
    stackname = context['stackname']

    context_handler.write_context(stackname, context)
    cloudformation_template_file = write_cloudformation_template(stackname, cloudformation_template)
    terraform_template_file = write_terraform_template(stackname, terraform_template)
    return context, cloudformation_template_file, terraform_template_file

#
# update existing template
#


# can't add ExtDNS: it changes dynamically when we start/stop instances and should not be touched after creation
UPDATABLE_TITLE_PATTERNS = ['^CloudFront.*', '^ElasticLoadBalancer.*', '^EC2Instance.*', '.*Bucket$', '.*BucketPolicy', '^StackSecurityGroup$', '^ELBSecurityGroup$', '^CnameDNS.+$', 'FastlyDNS\\d+$', '^AttachedDB$', '^AttachedDBSubnet$', '^ExtraStorage.+$', '^MountPoint.+$', '^IntDNS.*$', '^ElastiCache.*$']

REMOVABLE_TITLE_PATTERNS = ['^CloudFront.*', '^CnameDNS\\d+$', 'FastlyDNS\\d+$', '^ExtDNS$', '^ExtraStorage.+$', '^MountPoint.+$', '^.+Queue$', '^EC2Instance.+$', '^IntDNS.*$', '^ElastiCache.*$', '^.+Topic$']
EC2_NOT_UPDATABLE_PROPERTIES = ['ImageId', 'Tags', 'UserData']

# CloudFormation is nicely chopped up into:
# * what to add
# * what to modify
# * what to remove
# What we see here is the new Terraform generated.tf file, containing all resources (just Fastly so far).
# We can do a diff with the current one which would already be an improvement, but ultimately the source of truth
# is changing it and running a terraform plan to see proposed changes. We should however roll it back if the user
# doesn't confirm.
class Delta(namedtuple('Delta', ['plus', 'edit', 'minus', 'terraform'])):
    @property
    def non_empty(self):
        return any([
            self.plus['Resources'],
            self.plus['Outputs'],
            self.edit['Resources'],
            self.edit['Outputs'],
            self.minus['Resources'],
            self.minus['Outputs'],
            self.terraform
        ])
_empty_cloudformation_dictionary = {'Resources': {}, 'Outputs': {}}
Delta.__new__.__defaults__ = (_empty_cloudformation_dictionary, _empty_cloudformation_dictionary, _empty_cloudformation_dictionary, None)

def template_delta(context):
    """given an already existing template, regenerates it and produces a delta containing only the new resources.

    Some the existing resources are treated as immutable and not put in the delta. Most that support non-destructive updates like CloudFront are instead included"""
    old_template = read_template(context['stackname'])
    template = json.loads(cloudformation.render_template(context))
    new_terraform_template_file = '{}'
    if context['fastly']:
        new_terraform_template_file = terraform.render(context)

    def _related_to_ec2(output):
        if 'Value' in output:
            if 'Ref' in output['Value']:
                return 'EC2Instance' in output['Value']['Ref']
            if 'Fn::GetAtt' in output['Value']:
                return 'EC2Instance' in output['Value']['Fn::GetAtt'][0]
        return False

    def _title_is_updatable(title):
        return len([p for p in UPDATABLE_TITLE_PATTERNS if re.match(p, title)]) > 0

    def _title_is_removable(title):
        return len([p for p in REMOVABLE_TITLE_PATTERNS if re.match(p, title)]) > 0

    # TODO: investigate if this is still necessary
    # start backward compatibility code
    # back for when EC2Instance was the title rather than EC2Instance1
    if 'EC2Instance' in old_template['Resources']:
        if 'ExtraStorage' in template['Resources']:
            template['Resources']['ExtraStorage']['Properties']['AvailabilityZone']['Fn::GetAtt'][0] = 'EC2Instance'
        if 'MountPoint' in template['Resources']:
            template['Resources']['MountPoint']['Properties']['InstanceId']['Ref'] = 'EC2Instance'
        if 'IntDNS' in template['Resources']:
            template['Resources']['IntDNS']['Properties']['ResourceRecords'][0]['Fn::GetAtt'][0] = 'EC2Instance'
        if 'ExtDNS' in template['Resources']:
            template['Resources']['ExtDNS']['Properties']['ResourceRecords'][0]['Fn::GetAtt'][0] = 'EC2Instance'
    # end backward compatibility code

    def _title_has_been_updated(title, section):
        # title was there before with a deprecated name, leave it alone
        # e.g. 'EC2Instance' rather than 'EC2Instance1'
        if not title in old_template[section]:
            return False

        title_in_old = dict(old_template[section][title])
        title_in_new = dict(template[section][title])
        # ignore UserData changes, it's not useful to update them and cause
        # a needless reboot
        if 'Type' in title_in_old:
            if title_in_old['Type'] == 'AWS::EC2::Instance':
                for property_name in EC2_NOT_UPDATABLE_PROPERTIES:
                    title_in_old['Properties'][property_name] = None
                    title_in_new['Properties'][property_name] = None
        return title_in_old != title_in_new

    def legacy_title(title):
        # some titles like EC2Instance1 were originally EC2Instance
        # however, no reason not to let EC2Instance2 be created?
        if title in ['EC2Instance1', 'ExtraStorage1', 'MountPoint1']:
            return title.strip('1')

    delta_plus_resources = {
        title: r for (title, r) in template['Resources'].items()
        if (title not in old_template['Resources']
            and (legacy_title(title) not in old_template['Resources'])
            and (title != 'EC2Instance'))
    }
    delta_plus_outputs = {
        title: o for (title, o) in template.get('Outputs', {}).items()
        if (title not in old_template.get('Outputs', {}) and not _related_to_ec2(o))
    }

    delta_edit_resources = {
        title: r for (title, r) in template['Resources'].items()
        if (_title_is_updatable(title) and _title_has_been_updated(title, 'Resources'))
    }
    delta_edit_outputs = {
        title: o for (title, o) in template.get('Outputs', {}).items()
        if (_title_is_updatable(title) and _title_has_been_updated(title, 'Outputs'))
    }

    delta_minus_resources = {r: v for r, v in old_template['Resources'].items() if r not in template['Resources'] and _title_is_removable(r)}
    delta_minus_outputs = {o: v for o, v in old_template.get('Outputs', {}).items() if o not in template.get('Outputs', {})}

    return Delta(
        {
            'Resources': delta_plus_resources,
            'Outputs': delta_plus_outputs,
        },
        {
            'Resources': delta_edit_resources,
            'Outputs': delta_edit_outputs,
        },
        {
            'Resources': delta_minus_resources,
            'Outputs': delta_minus_outputs,
        },
        new_terraform_template_file
    )

def merge_delta(stackname, delta):
    """Merges the new resources in delta in the local copy of the Cloudformation  template"""
    template = read_template(stackname)
    apply_delta(template, delta)
    write_cloudformation_template(stackname, json.dumps(template))
    write_terraform_template(stackname, delta.terraform)
    return template

def apply_delta(template, delta):
    for component in delta.plus:
        ensure(component in ["Resources", "Outputs"], "Template component %s not recognized" % component)
        data = template.get(component, {})
        data.update(delta.plus[component])
        template[component] = data
    for component in delta.edit:
        ensure(component in ["Resources", "Outputs"], "Template component %s not recognized" % component)
        data = template.get(component, {})
        data.update(delta.edit[component])
        template[component] = data
    for component in delta.minus:
        ensure(component in ["Resources", "Outputs"], "Template component %s not recognized" % component)
        for title in delta.minus[component]:
            del template[component][title]

def _current_cloudformation_template(stackname):
    "retrieves a template from the CloudFormation API, using it as the source of truth"
    conn = core.connect_aws_with_stack(stackname, 'cfn')
    return json.loads(conn.get_template(stackname)['GetTemplateResponse']['GetTemplateResult']['TemplateBody'])

def download_cloudformation_template(stackname):
    write_cloudformation_template(stackname, json.dumps(_current_cloudformation_template(stackname)))

def regenerate_stack(stackname, **more_context):
    current_context = context_handler.load_context(stackname)
    download_cloudformation_template(stackname)
    (pname, instance_id) = core.parse_stackname(stackname)
    more_context['stackname'] = stackname # TODO: purge this crap
    more_context['alt-config'] = instance_id
    context = build_context(pname, existing_context=current_context, **more_context)
    delta = template_delta(context)
    return context, delta, current_context
