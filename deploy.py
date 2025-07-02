#!/usr/bin/env python3
import boto3
import json
import time
import sys
import os
import argparse
import requests
from botocore.exceptions import ClientError, NoCredentialsError, WaiterError

class EC2Deployer:
    def __init__(self, stage='dev'):
        self.stage = stage.lower()
        self.sts_client = boto3.client('sts')
        self.config = self.load_config()
        self.ssm_client = boto3.client('ssm')
        
        try:
            self.ec2_client = boto3.client('ec2')
            self.s3_client = boto3.client('s3')
            self.iam_client = boto3.client('iam')
        except NoCredentialsError:
            print("ERROR: AWS credentials not found in environment variables")
            sys.exit(1)
        
        self.instance_id = None
        self.bucket_name = None
        
    def load_config(self):
        """Load configuration based on stage parameter"""
        config_file = f"{self.stage}_config.json"
        
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            # Use defaults if config not available
            return {
                "instance_type": "t2.micro",
                "ami_id": "ami-0f5ee92e2d63afc18",
                "github_repo": "https://github.com/techeazy-consulting/techeazy-devops",
                "stop_after_minutes": 60
            }
    
    def create_iam_roles(self):
        """Create two IAM roles as per assignment requirements"""
        
        # --- NEW: Get the ARN of the user running the script ---
        try:
            user_arn = self.sts_client.get_caller_identity()['Arn']
            print(f"Running script as user: {user_arn}")
        except ClientError as e:
            print(f"Could not determine user identity: {e}. Cannot create assume role policy correctly.")
            sys.exit(1)

        # Trust policy allowing ONLY EC2 instances to assume a role
        ec2_only_trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": { "Service": "ec2.amazonaws.com" },
                    "Action": "sts:AssumeRole"
                }
            ]
        }
        
        # --- MODIFIED: Trust policy for the read role ---
        # This policy allows BOTH the EC2 service and the user running the script to assume it.
        read_role_trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "ec2.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                },
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": user_arn
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }
        
        # Role 1a: S3 Read Only Access, scoped to the specific bucket
        read_role_name = f"S3ReadOnlyRole-{self.stage}"
        read_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [ "s3:GetObject", "s3:ListBucket" ],
                    "Resource": [
                        f"arn:aws:s3:::{self.config['bucket_name']}",
                        f"arn:aws:s3:::{self.config['bucket_name']}/*"
                    ]
                }
            ]
        }
        
        # Role 1b: S3 Create Bucket and Upload Only Access
        upload_role_name = f"S3UploadOnlyRole-{self.stage}"
        upload_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [ "s3:PutObject" ],
                    "Resource": [ f"arn:aws:s3:::{self.config['bucket_name']}/*" ]
                },
                {
                    "Effect": "Allow",
                    "Action": [ "s3:CreateBucket" ],
                    "Resource": "arn:aws:s3:::*"
                }
            ]
        }
        
        try:
            # --- Create Read Only Role with the MODIFIED trust policy ---
            try:
                self.iam_client.create_role(
                    RoleName=read_role_name,
                    AssumeRolePolicyDocument=json.dumps(read_role_trust_policy) # Use the new policy
                )
                print(f"Created IAM role: {read_role_name}")
            except ClientError as e:
                if e.response['Error']['Code'] == 'EntityAlreadyExists':
                    print(f"Role {read_role_name} already exists. NOTE: Trust policy may need manual update in IAM console if it has changed.")
                else:
                    raise
            
            self.iam_client.put_role_policy(
                RoleName=read_role_name,
                PolicyName='S3ReadOnlyPolicy',
                PolicyDocument=json.dumps(read_policy)
            )
            
            # --- Create Upload Only Role with the original EC2-only trust policy ---
            try:
                self.iam_client.create_role(
                    RoleName=upload_role_name,
                    AssumeRolePolicyDocument=json.dumps(ec2_only_trust_policy) # Use the EC2-only policy
                )
                print(f"Created IAM role: {upload_role_name}")
            except ClientError as e:
                if e.response['Error']['Code'] == 'EntityAlreadyExists':
                    print(f"Role {upload_role_name} already exists")
                else:
                    raise
            
            print(f"Attaching SSM policy to role {upload_role_name}...")
            self.iam_client.attach_role_policy(
                RoleName=upload_role_name,
                PolicyArn='arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
            )

            self.iam_client.put_role_policy(
                RoleName=upload_role_name,
                PolicyName='S3UploadOnlyPolicy',
                PolicyDocument=json.dumps(upload_policy)
            )
            
            profile_name = f"EC2-S3-Upload-Profile-{self.stage}"
            try:
                self.iam_client.create_instance_profile(
                    InstanceProfileName=profile_name
                )
                print(f"Created instance profile: {profile_name}")
            except ClientError as e:
                if e.response['Error']['Code'] == 'EntityAlreadyExists':
                    print(f"Instance profile {profile_name} already exists")
                else:
                    raise
            
            try:
                self.iam_client.add_role_to_instance_profile(
                    InstanceProfileName=profile_name,
                    RoleName=upload_role_name
                )
                print(f"Added role {upload_role_name} to instance profile {profile_name}")
            except ClientError as e:
                if e.response['Error']['Code'] == 'LimitExceeded':
                    print(f"Role {upload_role_name} already attached to instance profile {profile_name}")
                else:
                    raise
            
            return profile_name, read_role_name
            
        except ClientError as e:
            print(f"An error occurred during IAM role/profile creation: {e}")
            return None, None
        
    def create_s3_bucket(self):
        """Create private S3 bucket with configurable name"""
        if 'bucket_name' not in self.config:
            print("ERROR: bucket_name not provided in config. Terminating.")
            sys.exit(1)
        
        self.bucket_name = self.config['bucket_name']
        
        try:
            # Create bucket
            self.s3_client.create_bucket(
                Bucket=self.bucket_name,
                CreateBucketConfiguration={'LocationConstraint': 'ap-south-1'}
            )
            print(f"Created S3 bucket: {self.bucket_name}")
            
            # Make bucket private (block public access)
            self.s3_client.put_public_access_block(
                Bucket=self.bucket_name,
                PublicAccessBlockConfiguration={
                    'BlockPublicAcls': True,
                    'IgnorePublicAcls': True,
                    'BlockPublicPolicy': True,
                    'RestrictPublicBuckets': True
                }
            )
            print(f"Set bucket {self.bucket_name} as private")
            
            # Add lifecycle rule to delete logs after 7 days
            lifecycle_config = {
                'Rules': [
                    {
                        'ID': 'DeleteLogsAfter7Days',
                        'Status': 'Enabled',
                        'Filter': {'Prefix': 'logs/'},
                        'Expiration': {'Days': 7}
                    }
                ]
            }
            
            self.s3_client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket_name,
                LifecycleConfiguration=lifecycle_config
            )
            print("Added S3 lifecycle rule to delete logs after 7 days")
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'BucketAlreadyOwnedByYou':
                print(f"Bucket {self.bucket_name} already exists")
            else:
                print(f"Error creating S3 bucket: {e}")
                sys.exit(1)
    
    def create_security_group(self):
        """Create or update security group to ensure port 22, 80, and 8080 access."""
        sg_name = f"techeazy-sg-{self.stage}"
        
        try:
            # Check if security group already exists
            response = self.ec2_client.describe_security_groups(
                Filters=[{'Name': 'group-name', 'Values': [sg_name]}]
            )
            
            if response['SecurityGroups']:
                sg = response['SecurityGroups'][0]
                sg_id = sg['GroupId']
                print(f"Using existing security group: {sg_id}")

                # Check existing permissions
                existing_ports = []
                for perm in sg.get('IpPermissions', []):
                    if 'FromPort' in perm and perm.get('IpProtocol') == 'tcp':
                        for ip_range in perm.get('IpRanges', []):
                            if ip_range.get('CidrIp') == '0.0.0.0/0':
                                existing_ports.append(perm['FromPort'])
                
                ports_to_add = []
                for port in [22, 80, 8080]:
                    if port not in existing_ports:
                        ports_to_add.append(port)
                
                if ports_to_add:
                    print(f"Authorizing missing ports: {ports_to_add}")
                    new_permissions = [
                        {
                            'IpProtocol': 'tcp',
                            'FromPort': port,
                            'ToPort': port,
                            'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                        } for port in ports_to_add
                    ]
                    self.ec2_client.authorize_security_group_ingress(
                        GroupId=sg_id,
                        IpPermissions=new_permissions
                    )
                else:
                    print("All required ports (22, 80, 8080) are already authorized.")
                    
                return sg_id

            # If it doesn't exist, create it with all rules
            print(f"Creating new security group: {sg_name}")
            response = self.ec2_client.create_security_group(
                GroupName=sg_name,
                Description=f'Security group for port 22, 80 and 8080 access'
            )
            
            sg_id = response['GroupId']
            
            # Add all rules
            self.ec2_client.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        'IpProtocol': 'tcp',
                        'FromPort': 80,
                        'ToPort': 80,
                        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                    },
                    {
                        'IpProtocol': 'tcp',
                        'FromPort': 8080,
                        'ToPort': 8080,
                        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                    },
                    {
                        'IpProtocol': 'tcp',
                        'FromPort': 22,
                        'ToPort': 22,
                        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                    }
                ]
            )
            
            print(f"Created security group {sg_id} with rules for ports 22, 80, 8080.")
            return sg_id
            
        except ClientError as e:
            print(f"Error managing security group: {e}. Using default security group.")
            return None
    
    def launch_instance(self, instance_profile_name):
        """Spins up an EC2 instance of a specific type"""
        
        # Create or get security group
        sg_id = self.create_security_group()
        
        # Enhanced user_data with S3 upload functionality
        user_data = f"""#!/bin/bash
# Update package list and install all dependencies
apt-get update -y
apt-get install -y openjdk-21-jdk maven git awscli

# Define home directories
UBUNTU_HOME="/home/ubuntu"
APP_DIR="$UBUNTU_HOME/app"

# Clone repo from GitHub
cd $UBUNTU_HOME
git clone {self.config['github_repo']}.git app

# --- FIX APPLICATION SOURCE CODE ON THE FLY ---
CONTROLLER_FILE="$APP_DIR/src/main/java/com/techeazy/devops/controller/TestController.java"
if [ -f "$CONTROLLER_FILE" ]; then
    sed -i '/@GetMapping/{{N; /SampleRequest/s/@GetMapping/@GetMapping("\\/sample")/}}' "$CONTROLLER_FILE"
    echo "Applied on-the-fly patch to $CONTROLLER_FILE" >> $UBUNTU_HOME/build.log
fi
# --- END OF FIX ---

# Build and run the actual Java application on port 80
cd $APP_DIR
if [ -f "pom.xml" ]; then
    export HOME=/root # Required for mvnw as root
    echo "Building with Maven (HOME=$HOME)..." >> $UBUNTU_HOME/build.log
    
    chmod +x ./mvnw
    ./mvnw clean package -DskipTests >> $UBUNTU_HOME/build.log 2>&1
    
    # Check if JAR was built successfully
    if ls target/*.jar 1> /dev/null 2>&1; then
        # The app will run on port 80 because of application.properties
        echo "JAR found, starting application..." >> $UBUNTU_HOME/build.log
        nohup java -jar target/*.jar > $UBUNTU_HOME/app.log 2>&1 &
    else
        echo "No JAR file found in target directory after build. See build.log for details." >> $UBUNTU_HOME/build.log
    fi
else
    echo "No pom.xml found in repository" >> $UBUNTU_HOME/build.log
fi

# Create shutdown script for log upload
cat > $UBUNTU_HOME/upload_logs.sh << 'EOF'
#!/bin/bash
# Upload EC2 logs to S3 bucket
aws s3 cp /var/log/cloud-init.log s3://{self.bucket_name}/logs/cloud-init.log || echo "Failed to upload cloud-init.log"
aws s3 cp /home/ubuntu/build.log s3://{self.bucket_name}/logs/build.log || echo "Failed to upload build.log"
aws s3 cp /home/ubuntu/app.log s3://{self.bucket_name}/app/logs/app.log || echo "Failed to upload app.log"
EOF

chmod +x $UBUNTU_HOME/upload_logs.sh
"""
        
        try:
            # Prepare instance parameters
            instance_params = {
                'ImageId': self.config['ami_id'],
                'MinCount': 1,
                'MaxCount': 1,
                'InstanceType': self.config['instance_type'],
                'UserData': user_data,
                'IamInstanceProfile': {'Name': instance_profile_name}
            }
            
            # Add key pair if specified in config
            if 'aws_key' in self.config:
                instance_params['KeyName'] = self.config['aws_key']
                print(f"Using key pair: {self.config['aws_key']}")
            
            # Add security group
            if sg_id:
                instance_params['SecurityGroupIds'] = [sg_id]
            else:
                instance_params['SecurityGroups'] = ['default']
            
            response = self.ec2_client.run_instances(**instance_params)
            
            self.instance_id = response['Instances'][0]['InstanceId']
            print(f"Instance launched: {self.instance_id}")
            return self.instance_id
            
        except ClientError as e:
            print(f"Error launching instance: {e}")
            sys.exit(1)
    
    def wait_for_instance(self):
        """Wait for instance to be running and get public IP"""
        waiter = self.ec2_client.get_waiter('instance_running')
        waiter.wait(InstanceIds=[self.instance_id])
        
        response = self.ec2_client.describe_instances(InstanceIds=[self.instance_id])
        instance = response['Reservations'][0]['Instances'][0]
        public_ip = instance.get('PublicIpAddress')
        
        print(f"Instance running. Public IP: {public_ip}")
        return public_ip
    
    def test_reachability(self, public_ip):
        """Tests if the Java application is reachable via port 80."""
        print("Testing if Java app is reachable via port 80...")
        
        # Wait for the full Maven build and Java app startup time.
        print("Waiting 300 seconds (5 minutes) for the full deployment...")
        time.sleep(300)
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # The root endpoint from TestController.java should return "Successfully Deployed"
                test_url = f"http://{public_ip}:80/"
                print(f"Attempting to connect to {test_url}...")
                response = requests.get(test_url, timeout=10)
                
                # Check for the Java application's specific response
                if response.status_code == 200 and "Successfully Deployed" in response.text:
                    print(f"SUCCESS: Java app is reachable on port 80. Response: {response.text}")
                    return True
                else:
                    print(f"Attempt {attempt + 1} failed with status code: {response.status_code}")

            except requests.exceptions.RequestException as e:
                print(f"Attempt {attempt + 1} failed with exception: {e}")
            
            if attempt < max_attempts - 1:
                print("Retrying in 60 seconds...")
                time.sleep(60)
        
        print("ERROR: Java application is not reachable on port 80.")
        return False
    
    def upload_logs_and_stop_instance(self):
        """Upload logs to S3 using SSM Run Command and then stop the instance."""
        stop_after = self.config.get('stop_after_minutes', 60)
        print(f"Deployment complete. Instance will be processed for log upload and shutdown in {stop_after} minutes.")
        
        # Wait for the configured duration
        time.sleep(stop_after * 60)
        
        try:
            # Execute log upload script on the instance before stopping
            print("Executing log upload script on the instance via SSM...")
            command = "/home/ubuntu/upload_logs.sh"
            
            response = self.ssm_client.send_command(
                InstanceIds=[self.instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={'commands': [command]},
                Comment=f'Upload logs for instance {self.instance_id}'
            )
            command_id = response['Command']['CommandId']
            
            # Wait for the command to complete
            print(f"Waiting for SSM command {command_id} to complete...")
            time.sleep(15) # Give it a moment to run
            
            waiter = self.ssm_client.get_waiter('command_executed')
            waiter.wait(
                CommandId=command_id,
                InstanceId=self.instance_id,
                WaiterConfig={'Delay': 5, 'MaxAttempts': 10}
            )
            print("Log upload command finished.")

            # Stop the instance
            print(f"Stopping instance {self.instance_id}...")
            self.ec2_client.stop_instances(InstanceIds=[self.instance_id])
            print(f"Instance {self.instance_id} is stopping.")
            
        except ClientError as e:
            print(f"An error occurred during log upload or instance stop: {e}")
        except WaiterError as e: # <-- CORRECTED THIS LINE
            print(f"Error waiting for SSM command to complete: {e}")
            print("Proceeding with instance stop anyway.")
            self.ec2_client.stop_instances(InstanceIds=[self.instance_id])
    
    def verify_s3_access(self, read_role_name):
        """Assume role 1.a and use it to verify that files can be listed in the S3 bucket."""
        print(f"\n--- Verifying S3 access using role: {read_role_name} ---")
        try:
            # Get the ARN of the role
            role_response = self.iam_client.get_role(RoleName=read_role_name)
            role_arn = role_response['Role']['Arn']

            # Assume the read-only role
            print(f"Attempting to assume role {role_arn}...")
            assumed_role = self.sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName="S3VerifyReadAccessSession"
            )
            
            # Create a new S3 client using the temporary credentials from the assumed role
            temp_credentials = assumed_role['Credentials']
            s3_client_as_role = boto3.client(
                's3',
                aws_access_key_id=temp_credentials['AccessKeyId'],
                aws_secret_access_key=temp_credentials['SecretAccessKey'],
                aws_session_token=temp_credentials['SessionToken'],
            )
            print("Successfully assumed role. Now listing objects in the bucket...")

            # Use the new client to list objects
            response = s3_client_as_role.list_objects_v2(Bucket=self.bucket_name)
            
            if 'Contents' in response and response['Contents']:
                print(f"SUCCESS: Files can be listed in bucket {self.bucket_name} using the read-only role.")
                print("Files found:")
                for obj in response['Contents']:
                    print(f"  - {obj['Key']} (Size: {obj['Size']} bytes)")
            else:
                print(f"SUCCESS: The read-only role can access the bucket {self.bucket_name}, but it is currently empty.")
                
        except ClientError as e:
            print(f"ERROR: Failed to verify S3 access using the read-only role: {e}")
        
    def deploy(self):
        """Main deployment process"""
        # Create IAM roles
        instance_profile_name, read_role_name = self.create_iam_roles()
        if not instance_profile_name:
            print("ERROR: Failed to create IAM roles")
            sys.exit(1)
        
        # Create S3 bucket
        self.create_s3_bucket()
        
        # Wait for instance profile to be ready
        print("Waiting for instance profile to be ready...")
        time.sleep(10)
        
        # 1. Spins up an EC2 instance with IAM role
        self.launch_instance(instance_profile_name)
        
        # 2. Wait for instance (installs dependencies & clones repo via user data)
        public_ip = self.wait_for_instance()
        
        # 3. Tests if app is reachable via port 80
        self.test_reachability(public_ip)
        
        # 4. Upload logs and stop the instance
        self.upload_logs_and_stop_instance()
        
        # 5. Verify S3 access using read role
        self.verify_s3_access(read_role_name)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', default='dev', help='Stage parameter (dev/prod)')
    args = parser.parse_args()
    
    # Check environment variables (no secrets in repo)
    if not os.getenv('AWS_ACCESS_KEY_ID') or not os.getenv('AWS_SECRET_ACCESS_KEY'):
        print("ERROR: AWS credentials not found in environment variables")
        sys.exit(1)
    
    deployer = EC2Deployer(args.stage)
    deployer.deploy()

if __name__ == "__main__":
    main()