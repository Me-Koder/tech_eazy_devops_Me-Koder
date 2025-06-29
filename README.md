# TechEazy DevOps Assignment

This project automates the deployment of a Java application to AWS EC2 using Python scripts.

## Features

- ✅ Automated EC2 instance provisioning
- ✅ Java 21 installation and setup
- ✅ GitHub repository cloning and deployment
- ✅ Application accessibility testing on port 80
- ✅ Automatic instance termination for cost optimization
- ✅ Stage-based configuration (Dev/Prod)
- ✅ Secure credential management via environment variables

## Prerequisites

1. **AWS Account**: Sign up for AWS Free Tier
2. **Python 3.7+**: Required for running the deployment script
3. **AWS Credentials**: Set as environment variables

## Setup Instructions

### 1. Clone Repository

```bash
git clone https://github.com/Me-Koder/tech_eazy_devops_Me-Koder.git
cd tech_eazy_devops_Me-Koder
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Environment Variables

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_DEFAULT_REGION="ap-south-1"
```

### 4. Run Deployment

#### For Development:
```bash
python deploy.py --stage dev
```

#### For Production:
```bash
python deploy.py --stage prod
```

