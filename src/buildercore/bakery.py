__author__ = 'Luke Skibinski <l.skibinski@elifesciences.org>'
__description__ = """Module that deals with AMI baking!

We bake new AMIs to avoid long deployments and the occasional
runtime bugs that crop up while building brand new machines."""

from buildercore import core, utils, bootstrap, config

def ami_name(stackname):
    # elife-api.2015-12-31
    return "%s.%s" % (core.project_name_from_stackname(stackname), utils.ymd())

@core.requires_active_stack
def create_ami(stackname):
    "creates an AMI from the running stack"
    with core.stack_conn(stackname, username=config.BOOTSTRAP_USER):
        bootstrap.prep_ec2_instance()
    ec2 = core.find_ec2_instances(stackname)[0]
    kwargs = {
        'instance_id': ec2.id,
        'name': ami_name(stackname),
        'no_reboot': True,
        #'dry_run': True
    }
    conn = core.connect_aws_with_stack(stackname, 'ec2')
    ami_id = conn.create_image(**kwargs)

    # image.__dict__ == {'root_device_type': u'ebs', 'ramdisk_id': None, 'id': u'ami-6bc99d0e', 'owner_alias': None, 'billing_products': [], 'tags': {}, 'platform': None, 'state': u'pending', 'location': u'512686554592/elife-lax.2015-10-15', 'type': u'machine', 'virtualization_type': u'hvm', 'sriov_net_support': u'simple', 'architecture': u'x86_64', 'description': None, 'block_device_mapping': {}, 'kernel_id': None, 'owner_id': u'512686554592', 'is_public': False, 'instance_lifecycle': None, 'creationDate': u'2015-10-15T16:07:21.000Z', 'name': u'elife-lax.2015-10-15', 'hypervisor': u'xen', 'region': RegionInfo:us-east-1, 'item': u'\n        ', 'connection': EC2Connection:ec2.us-east-1.amazonaws.com, 'root_device_name': None, 'ownerId': u'512686554592', 'product_codes': []}
    def is_pending():
        image = conn.get_all_images(image_ids=[ami_id])[0]
        return image.state == 'pending'
    utils.call_while(is_pending, update_msg="Waiting for AWS to bake AMI %s ... " % ami_id)
    return str(ami_id) # this should be used to update the stack templates
