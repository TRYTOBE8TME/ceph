#!/usr/bin/python3

import logging as log
#import time
import subprocess
import json
import boto3
from botocore.client import Config
import os

"""
Rgw manual and dynamic resharding  testing against a running instance
"""
# The test cases in this file have been annotated for inventory.
# To extract the inventory (in csv format) use the command:
#
#   grep '^ *# TESTCASE' | sed 's/^ *# TESTCASE //'
#
#

log.basicConfig(level=log.DEBUG)
log.getLogger('botocore').setLevel(log.CRITICAL)
log.getLogger('boto3').setLevel(log.CRITICAL)
log.getLogger('urllib3').setLevel(log.CRITICAL)

""" Constants """
USER = 'rgw_datacache_user'
DISPLAY_NAME = 'DatacacheUser'
ACCESS_KEY = 'NX5QOQKC6BH2IDN8HC7A'
SECRET_KEY = 'LnEsqNNqZIpkzauboDcLXLcYaWwLQ3Kop0zAnKIn'
BUCKET_NAME = 'datacachebucket'
OBJECT_NAME = 'datacachebucket'


def exec_cmd(cmd):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        out, err = proc.communicate()
        if proc.returncode == 0:
            log.info('command succeeded')
            if out is not None: log.info(out)
            return out
        else:
            raise Exception("error: %s \nreturncode: %s" % (err, proc.returncode))
    except Exception as e:
        log.error('command failed')
        log.error(e)
        return False

def get_radosgw_port():
    out = exec_cmd('sudo netstat -nltp | grep radosgw')
    log.debug('output: %s' % out)
    x = out.decode('utf8').split(" ")
    port = [i for i in x if ':' in i][0].split(':')[1]
    log.info('radosgw port: %s' % port)
    return port

def generate_random_file(filename,size):
    with open('%s'%filename, 'wb') as fout:
        fout.write(os.urandom(size))
    log.debug('file %s size %s written', filename, size)
    return fout

def main():
    """
    execute manual and dynamic resharding commands
    """
    log.debug('ALI is here')
    out = exec_cmd('pwd')
    x = out.decode('utf8')
    log.debug("ALI out is: %s", x)
    cache_dir = os.environ['RGW_DATACACHE_PATH']
    log.debug("ALI cache dir is: %s", cache_dir)

    # create user
    exec_cmd('radosgw-admin user create --uid %s --display-name %s --access-key %s --secret %s' % (USER, DISPLAY_NAME, ACCESS_KEY, SECRET_KEY))

    def get_boto3_client(portnum, ssl, proto):
        endpoint = proto + '://localhost:' + portnum
        client = boto3.client(service_name='s3',
                              aws_access_key_id=ACCESS_KEY,
                              aws_secret_access_key=SECRET_KEY,
                              endpoint_url=endpoint,
                              use_ssl=ssl,
                              config=Config(signature_version='s3v4'),
                              )
        return client

    port = get_radosgw_port()

    if port == '80':
        client = get_boto3_client(port, ssl=False, proto='http')
    elif port == '443':
        client = get_boto3_client(port, ssl=True, proto='https')

    # create a bucket
    client.create_bucket(Bucket=BUCKET_NAME)
    file_name = '7M.dat'
    f = generate_random_file(file_name, 1024*1024*7)
    client.put_object(Bucket=BUCKET_NAME, Key=OBJECT_NAME, Body=f)
    client.get_object(Bucket=BUCKET_NAME, Key=OBJECT_NAME)

    cmd = exec_cmd('radosgw-admin object stat --bucket=%s --object=%s'
                   % (BUCKET_NAME, OBJECT_NAME))

    json_op = json.loads(cmd)
    cached_object_name = json_op['manifest']['prefix']
    log.debug("Cached object name %s", cached_object_name)

    # create user -- done
    # create bucket -- done
    # put 7 mb object
    # get 7 mb object
    # run object stat
    # get name of cached object and check if it exists in the cache
    # check to see if cached object is in ceph
    # log TEST DONE!

    #log.debug('bucket name %s', json_op[0]['bucket_name'])
    #assert json_op[0]['bucket_name'] == BUCKET_NAME1
    #assert json_op[0]['new_num_shards'] == num_shards_expected

    # TESTCASE 'reshard-process','reshard','','process bucket resharding','succeeds'
    #log.debug(' test: reshard process')
    #cmd = exec_cmd('radosgw-admin reshard process')
    #time.sleep(5)
    # check bucket shards num
    #bucket_stats1 = get_bucket_stats(BUCKET_NAME1)
    #bucket_stats1.get_num_shards()
    #if bucket_stats1.num_shards != num_shards_expected:
        #log.error("Resharding failed on bucket %s. Expected number of shards are not created" % BUCKET_NAME1)

    # Clean up
    log.debug("Deleting bucket %s", BUCKET_NAME)
    client.delete_bucket(Bucket=BUCKET_NAME)
    client.delete_object(Bucket=BUCKET_NAME, Key=OBJECT_NAME)
    log.debug("Bucket %s deleted", BUCKET_NAME)


main()
log.info("Completed Datacache tests")
