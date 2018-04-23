import json
import os
from os.path import join
from python_terraform import Terraform
from buildercore.utils import ensure
from .config import BUILDER_BUCKET, BUILDER_REGION, TERRAFORM_DIR, ConfigurationError
from .context_handler import only_if

RESOURCE_TYPE_FASTLY = 'fastly_service_v1'
RESOURCE_NAME_FASTLY = 'fastly-cdn'

def render(context):
    if not context['fastly']:
        return '{}'

    all_allowed_subdomains = context['fastly']['subdomains'] + context['fastly']['subdomains-without-dns']
    tf_file = {
        'resource': {
            RESOURCE_TYPE_FASTLY: {
                # must be unique but only in a certain context like this, use some constants
                RESOURCE_NAME_FASTLY: {
                    'name': context['stackname'],
                    'domain': [
                        {'name': subdomain} for subdomain in all_allowed_subdomains
                    ],
                    'backend': {
                        'address': context['full_hostname'],
                        'name': context['stackname'],
                        'port': 443,
                        'use_ssl': True,
                        'ssl_cert_hostname': context['full_hostname'],
                        'ssl_check_cert': True,
                    },
                    'force_destroy': True
                }
            }
        },
    }
    return json.dumps(tf_file)

def init(stackname):
    working_dir = join(TERRAFORM_DIR, stackname) # ll: ./.cfn/terraform/project--prod/
    terraform = Terraform(working_dir=working_dir)
    with open('%s/backend.tf' % working_dir, 'w') as fp:
        fp.write(json.dumps({
            'terraform': {
                'backend': {
                    's3': {
                        'bucket': BUILDER_BUCKET,
                        'key': 'terraform/%s.tfstate' % stackname,
                        'region': BUILDER_REGION,
                    },
                },
            },
        }))
    terraform.init(input=False, capture_output=False, raise_on_error=True)
    return terraform

@only_if('fastly')
def update(stackname, context):
    ensure('FASTLY_API_KEY' in os.environ, "a FASTLY_API_KEY environment variable is required to provision Fastly resources. See https://manage.fastly.com/account/personal/tokens", ConfigurationError)
    terraform = init(stackname)
    terraform.apply(input=False, capture_output=False, raise_on_error=True)

@only_if('fastly')
def destroy(stackname, context):
    terraform = init(stackname)
    terraform.destroy(input=False, capture_output=False, raise_on_error=True)
    # TODO: also destroy files
