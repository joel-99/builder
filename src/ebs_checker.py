from fabric.api import run, task, get

import os
import datetime
from dateutil.tz import tzutc
import pickle
import boto3

'''
list ebs volumes that are in state
for each
    - attach the volume
    - mount the volume
    - emit list of volume contents
    - emit volume id
'''

def contents(fname):
    if os.path.exists(fname):
        return pickle.load(open(fname, 'rb'))

def save_contents(fname, fn, *args, **kwargs):
    results = fn(*args, **kwargs)
    pickle.dump(results, open(fname, 'wb'))
    return results

def me():
    return "i-01bf2487b60e091b8" # basebox--2017-11-01

#
#
#

def client():
    return boto3.client('ec2')    

def vlist():
    return client().describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}])['Volumes']

def attach_volume(vol):
    dev = '/dev/xvdh'
    return dev
    client().attach_volume(
        Device=dev,
        InstanceId=me(),
        Volume=vol['VolumeId'],
    )
    # todo: poll until attached
    return dev

def mount_volume(dev):
    cmd = "mount %s /mnt" % dev
    print(cmd)
    return "/mnt"

def scan_mountpoint(mountpoint):
    cmd = "cd %s && find ." % mountpoint
    print(cmd)
    return []

def scanned_list():
    return filter(lambda fname: fname.endswith('.scan'), os.listdir('.'))

def spy(form):
    print(form)
    return form

#
#
#

@task()
def scan():
    volumes = contents('vlist.pkl') or save_contents('vlist.pkl', vlist)
    print("%s volumes to scan" % len(volumes))

    for vol in volumes:
        if "%s.scan" % vol['VolumeId'] not in scanned_list():
            device = attach_volume(vol)
            mountpoint = mount_volume(device)
            scanlist = save_contents('%s.scan' % vol['VolumeId'], scan_mountpoint, mountpoint)
        else:
            print("scan for %s found, skipping" % vol['VolumeId'])
        

