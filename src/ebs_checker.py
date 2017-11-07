# -*- coding: utf-8 -*-

import time
from fabric.api import run, sudo as _sudo, task, get
from buildercore import core
from buildercore.utils import ensure
import os
import datetime
from dateutil.tz import tzutc
import pickle
import boto3
import logging


logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)

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

def load_contents(fname):
    return pickle.load(open(fname, 'rb'))

def me():
    return "i-01bf2487b60e091b8" # basebox--2017-11-01

def stackname():
    return 'basebox--2017-11-01'

#
#
#

def client():
    return boto3.client('ec2')    

def vlist():
    return client().describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}])['Volumes']

def volume_attached(volid):
    ec2 = boto3.resource('ec2').Instance(me())    
    attached_volumes = map(lambda dev: dev['Ebs']['VolumeId'], ec2.block_device_mappings)
    print 'attached:',attached_volumes
    return volid in attached_volumes

def detach_volume(vol):
    if volume_attached(vol['VolumeId']):
        vol = boto3.resource('ec2').Volume(vol['VolumeId'])
        return vol.detach_from_instance(
            Device='/dev/xvdh',
            InstanceId=me(),
        )
    print "volume not attached, not de-taching"
        

def attach_volume(vol):
    dev = '/dev/xvdh'
    if volume_attached(vol['VolumeId']):
        print 'volume already attached'
        return dev + "1"
    
    resp = client().attach_volume(
        Device=dev,
        InstanceId=me(),
        VolumeId=vol['VolumeId'],
    )
    if resp['State'] != 'attaching':
        print 'still attaching:',resp
    return dev + "1" # /dev/xvdh1

def sudo(*args, **kwargs):
    kwargs['quiet'] = True
    res = _sudo(*args, **kwargs)
    return res.return_code, res

def scan_mountpoint(mountpoint):
    rc, stdout = sudo("cd %s && find ." % mountpoint)
    ensure(rc == 0, "failed to scan")
    return stdout

def scanned_list():
    return filter(lambda fname: fname.endswith('.scan'), os.listdir('.'))


#
#
#

@task()
def print_scan():
    for scan in scanned_list():
        vol = scan.split('.')[0]
        fname = "%s.txt" % vol
        open(fname, 'w').write(load_contents(scan))

@task()
def scan():
    volumes = contents('vlist.pkl') or save_contents('vlist.pkl', vlist)
    print("%s volumes to scan" % len(volumes))

    for i, vol in enumerate(volumes):
        volid = vol['VolumeId']
        print
        print i+1,volid
        if "%s.scan" % volid in scanned_list():
            print "scan found, skipping"
            detach_volume(vol)
            continue

        device = attach_volume(vol)
        with core.stack_conn(stackname()):
            #if has_partitions(vol):
            #    mountpoint = mount_volume(device)
            #    scanlist = save_contents('%s.scan' % volid, scan_mountpoint, mountpoint)
            #    unmount_volume(vol)
            #else:
            #    print "no partitions found"
            print '---'
            print 'any key to scan'
            raw_input()
            mountpoint = "/mnt"
            print 'scanning'
            scanlist = save_contents('%s.scan' % volid, scan_mountpoint, mountpoint)
            print 'any key to continue to continue to next volume'
            raw_input()
            
        if volume_attached(volid):
            print 'detaching ... ',detach_volume(vol)
            print 'any key once detached'
            raw_input()
