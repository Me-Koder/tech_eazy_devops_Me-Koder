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
                "ami_id": "ami-0c02fb55956c7d316",
                "github_repo": "https://github.com/techeazy-consulting/techeazy-devops",
                "stop_after_minutes": 60
            }
    
    def launch_instance(self):
        """Spins up an EC2 instance of a specific type"""
        user_data = f"""#!/bin/bash
# Install Java 21
yum update -y
yum install -y java-21-amazon-corretto-devel

# Clone repo & deploy app from GitHub
cd /home/ec2-user
git clone {self.config['github_repo']}.git app
cd app

# Deploy app (assuming it's a Spring Boot app)
if [ -f "pom.xml" ]; then
    yum install -y maven
    mvn clean package -DskipTests
    nohup java -jar target/*.jar --server.port=80 > /dev/null 2>&1 &
fi
"""
        
        try:
            response = self.ec2_client.run_instances(
                ImageId=self.config['ami_id'],
                MinCount=1,
                MaxCount=1,
                InstanceType=self.config['instance_type'],
                UserData=user_data,
                SecurityGroups=['default']
            )
            
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
        """Tests if app is reachable via port 80"""
        print("Testing if app is reachable via port 80...")
        
        # Wait for app to start
        time.sleep(180)  # 3 minutes for app startup
        
        try:
            response = requests.get(f"http://{public_ip}:80", timeout=10)
            if response.status_code == 200:
                print("App is reachable via port 80")
                return True
        except:
            pass
        
        print("App is not reachable via port 80")
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