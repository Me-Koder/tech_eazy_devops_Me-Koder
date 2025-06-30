#!/usr/bin/env python3
import boto3
import json
import time
import sys
import os
import argparse
import requests
from botocore.exceptions import ClientError, NoCredentialsError

class EC2Deployer:
    def __init__(self, stage='dev'):
        self.stage = stage.lower()
        self.config = self.load_config()
        
        try:
            self.ec2_client = boto3.client('ec2')
        except NoCredentialsError:
            print("ERROR: AWS credentials not found in environment variables")
            sys.exit(1)
        
        self.instance_id = None
        
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

                # --- START: NEW LOGIC TO CHECK AND ADD RULES ---
                
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
                    
                # --- END: NEW LOGIC ---
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
    
    def launch_instance(self):
        """Spins up an EC2 instance of a specific type"""
        
        # Create or get security group
        sg_id = self.create_security_group()
        
# In deploy.py, inside the launch_instance method

        user_data = f"""#!/bin/bash
# Update package list and install all dependencies
apt-get update -y
apt-get install -y openjdk-21-jdk maven git python3-pip

# Define home directories
UBUNTU_HOME="/home/ubuntu"
APP_DIR="$UBUNTU_HOME/app"

# Clone repo from GitHub
cd $UBUNTU_HOME
git clone {self.config['github_repo']}.git app

# --- FIX APPLICATION SOURCE CODE ON THE FLY ---
CONTROLLER_FILE="$APP_DIR/src/main/java/com/techeazy/devops/controller/TestController.java"
if [ -f "$CONTROLLER_FILE" ]; then
    # CORRECTED: The curly braces for the sed command are now doubled {{ and }}
    # so that the Python f-string interprets them as literal characters.
    sed -i '/@GetMapping/{{N; /SampleRequest/s/@GetMapping/@GetMapping("\\/sample")/}}' "$CONTROLLER_FILE"
    echo "Applied on-the-fly patch to $CONTROLLER_FILE" >> $UBUNTU_HOME/build.log
fi
# --- END OF FIX ---
# Start simple test server on port 80
echo "<h1>TechEazy Test Server - Port 80 Working!</h1>" > $UBUNTU_HOME/index.html
cd $UBUNTU_HOME
nohup python3 -m http.server 80 > server.log 2>&1 &

# Build and run the actual Java application
cd $APP_DIR
if [ -f "pom.xml" ]; then
    # --- FIX MAVEN BUILD ---
    # Set the HOME environment variable for the root user, which is required by the mvnw script
    # to know where to create the .m2 directory for dependencies.
    export HOME=/root
    echo "Building with Maven (HOME=$HOME)..." >> $UBUNTU_HOME/build.log
    # --- END OF FIX ---
    
    chmod +x ./mvnw
    ./mvnw clean package -DskipTests >> $UBUNTU_HOME/build.log 2>&1
    
    # Check if JAR was built successfully
    if ls target/*.jar 1> /dev/null 2>&1; then
        echo "JAR found, starting application on port 8080..." >> $UBUNTU_HOME/build.log
        nohup java -jar target/*.jar --server.port=8080 > $UBUNTU_HOME/app.log 2>&1 &
    else
        echo "No JAR file found in target directory after build. See build.log for details." >> $UBUNTU_HOME/build.log
    fi
else
    echo "No pom.xml found in repository" >> $UBUNTU_HOME/build.log
fi
"""
        
        try:
            # Prepare instance parameters
            instance_params = {
                'ImageId': self.config['ami_id'],
                'MinCount': 1,
                'MaxCount': 1,
                'InstanceType': self.config['instance_type'],
                'UserData': user_data
            }
            
            # Add key pair if specified in config
            if 'key_name' in self.config:
                instance_params['KeyName'] = self.config['key_name']
                print(f"Using key pair: {self.config['key_name']}")
            
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
        """Tests if app is reachable via port 8080 (actual Java app)"""
        print("Testing if Java app is reachable via port 8080...")
        
        # Wait for app to start (Java app takes longer to compile and start)
        time.sleep(300)  # 5 minutes for Maven build and Java app startup
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # --- MODIFIED LINE ---
                # Test the specific "/sample" endpoint that we know exists after the patch.
                test_url = f"http://{public_ip}:8080/sample"
                print(f"Attempting to connect to {test_url}...")
                response = requests.get(test_url, timeout=10)
                # --- END OF MODIFICATION ---
                
                if response.status_code == 200:
                    print(f"SUCCESS: Java app is reachable via port 8080. Response: {response.text}")
                    return True
                else:
                    print(f"Attempt {attempt + 1} failed with status code: {response.status_code}")

            except requests.exceptions.RequestException as e:
                print(f"Attempt {attempt + 1} failed with exception: {e}")
            
            if attempt < max_attempts - 1:
                print("Retrying in 60 seconds...")
                time.sleep(60)  # Wait longer between attempts for Java app
        
        print("Java app is not reachable via port 8080")
        return False
        
    def stop_instance(self):
        """Stops the instance after a set time (for cost saving)"""
        stop_after = self.config.get('stop_after_minutes', 60)
        print(f"Instance will stop after {stop_after} minutes")
        
        time.sleep(stop_after * 60)
        
        try:
            self.ec2_client.stop_instances(InstanceIds=[self.instance_id])
            print(f"Instance {self.instance_id} stopped")
        except ClientError as e:
            print(f"Error stopping instance: {e}")
    
    def deploy(self):
        """Main deployment process"""
        # 1. Spins up an EC2 instance
        self.launch_instance()
        
        # 2. Wait for instance (installs dependencies & clones repo via user data)
        public_ip = self.wait_for_instance()
        
        # 3. Tests if app is reachable via port 80
        self.test_reachability(public_ip)
        
        # 4. Stops the instance after a set time
        self.stop_instance()

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