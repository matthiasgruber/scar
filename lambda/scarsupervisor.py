# SCAR - Serverless Container-aware ARchitectures
# Copyright (C) GRyCAP - I3M - UPV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import boto3
import json
import os
from os import listdir
from os.path import isfile, join
import re
from subprocess import call, check_output, STDOUT
import traceback

print('Loading function')

udocker_bin = "/tmp/udocker/udocker"
lambda_output = "/tmp/lambda-stdout.txt"
script = "/tmp/udocker/script.sh"
name = 'lambda_cont'
init_script_path = "/tmp/udocker/init_script.sh"

def prepare_environment():
    # Install udocker in /tmp
    call(["mkdir", "-p", "/tmp/udocker"])
    call(["cp", "/var/task/udocker", udocker_bin])
    call(["chmod", "u+rx", udocker_bin])
    call(["mkdir", "-p", "/tmp/home/.udocker"])
    if ('INIT_SCRIPT_PATH' in os.environ) and os.environ['INIT_SCRIPT_PATH']:
        call(["cp", "/var/task/init_script.sh", init_script_path])

def prepare_container(container_image):
    # Check if the container is already downloaded
    cmd_out = check_output([udocker_bin, "images"]).decode("utf-8")
    if container_image not in cmd_out:
        print("SCAR: Pulling container '%s' from dockerhub" % container_image)
        # If the container doesn't exist
        call([udocker_bin, "pull", container_image])
    else:
        print("SCAR: Container image '%s' already available" % container_image)
    # Download and create container
    cmd_out = check_output([udocker_bin, "ps"]).decode("utf-8")
    if name not in cmd_out:
        print("SCAR: Creating container with name '%s' based on image '%s'." % (name, container_image))
        call([udocker_bin, "create", "--name=%s" % name, container_image])
        # Set container execution engine to Fakechroot
        call([udocker_bin, "setup", "--execmode=F1", name])
    else:
        print("SCAR: Container '" + name + "' already available")

def get_global_variables():
    cont_variables = []
    for key in os.environ.keys():
        # Find global variables with the specified prefix
        if re.match("CONT_VAR_.*", key):
            cont_variables.append('--env')
            # Remove global variable prefix
            cont_variables.append(key.replace("CONT_VAR_", "") + '=' + os.environ[key])
    return cont_variables

def prepare_output(context):
    stdout = "SCAR: Log group name: %s\n" % context.log_group_name
    stdout += "SCAR: Log stream name: %s\n" % context.log_stream_name
    stdout += "---------------------------------------------------------------------------\n"
    return stdout

def create_file(content, path):
    with open(path, "w") as f:
        f.write(content)

def create_event_file(event, request_id):
    event_file_path = "/tmp/%s/" % request_id
    call(["mkdir", "-p", event_file_path])
    create_file(event, event_file_path + "/event.json")

def lambda_handler(event, context):
    print("SCAR: Received event: " + json.dumps(event))
    stdout = prepare_output(context)
    try:
        create_event_file(json.dumps(event), context.aws_request_id)
        bucket = S3_Bucket()
        bucket.download_input(event, context.aws_request_id)
        prepare_environment()
        prepare_container(os.environ['IMAGE_ID'])

        # Create container execution command
        command = [udocker_bin, "--quiet", "run"]
        container_dirs = ["-v", "/tmp", "-v", "/dev", "-v", "/proc", "--nosysdirs"]
        container_vars = ["--env", "REQUEST_ID=%s" % context.aws_request_id]
        command.extend(container_dirs)
        command.extend(container_vars)
        # Add global variables (if any)
        global_variables = get_global_variables()
        if global_variables:
            command.extend(global_variables)

        # Container running script
        if ('script' in event) and event['script']:
            create_file(event['script'], script)
            command.extend(["--entrypoint=/bin/sh %s" % script, name])
        # Container with args
        elif ('cmd_args' in event) and event['cmd_args']:
            args = map(lambda x: x.encode('ascii'), event['cmd_args'])
            command.append(name)
            command.extend(args)
        # Script to be executed every time (if defined)
        elif ('INIT_SCRIPT_PATH' in os.environ) and os.environ['INIT_SCRIPT_PATH']:
            command.extend(["--entrypoint=/bin/sh %s" % init_script_path, name])
        # Only container
        else:
            command.append(name)
        print("UDOCKER command: %s" % command)
        # Execute script
        call(command, stderr=STDOUT, stdout=open(lambda_output, "w"))

        stdout += check_output(["cat", lambda_output]).decode("utf-8")
        bucket.upload_output(context.aws_request_id)
    except Exception:
        stdout += "ERROR: Exception launched:\n %s" % traceback.format_exc()
    print(stdout)
    return stdout

class S3_Bucket():

    def get_s3_client(self):
        return boto3.client('s3')

    def download_input(self, event, request_id):
        if ('Records' in event) and event['Records']:
            for record in event['Records']:
                name = record['s3']['bucket']['name']
                key = record['s3']['object']['key']
                download_path = '/tmp/%s/input' % request_id
                print ("Downloading item in bucket %s with key %s" %(name, key))
                os.makedirs(os.path.dirname(download_path), exist_ok=True)
                self.get_s3_client().download_file(name, key, download_path)
                print(check_output(["ls", "-la", "/tmp/%s/input" % request_id]).decode("utf-8"))

    def upload_output(self, request_id):
        output_folder = "/tmp/%s/" % request_id
        output_files_path = self.get_all_files_in_directory(output_folder)
        for file_path in output_files_path:
            file_key = file_path.replace(output_folder,"")
            print ("Uploading file in bucket %s with key %s" % (bucket_name, file_key))
            self.get_s3_client().upload_file(file_path, bucket_name, file_key)
            print ("Changing ACLs for public-read for object in bucket %s with key %s" % (bucket_name, file_key))
            s3_resource = boto3.resource('s3')
            obj = s3_resource.Object(bucket_name, file_key)
            obj.Acl().put(ACL='public-read')

    def get_all_files_in_directory(self, dir_path):
        files = []
        for root, dirs, files in os.walk(os.path.abspath(dir_path)):
            for file in files:
                files.append(os.path.join(root, file))
        return files
