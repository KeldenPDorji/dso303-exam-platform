# DSO303 Assignment 2: Online Exams & Assignment Submission Platform

A highly available AWS platform for a university of 8,000 students and 400 staff to sit timed exams and submit
assignments online. Students sit timed MCQ + short-answer exams with **30-second autosave** and upload PDF/ZIP
assignments up to **50 MB**. Tutors create exams, mark and release results. Administrators manage users and schedules.
MCQs are auto-marked and emails go out asynchronously.

Everything is defined in **Terraform**, and a **push to `main` redeploys automatically** through GitHub Actions.

![Architecture](architecture/architecture.png)

| Target | How |
|---|---|
| 300–500 normal users, **3,000 exam starts in 5 min** | CloudFront edge cache, API Gateway + Lambda per-request scaling, RDS Proxy connection pooling, SQS buffering of marking and email |
| **99.9%** during exams, **survives loss of one AZ** | Every tier in 2 AZs, NAT gateway per AZ, RDS Multi-AZ automatic failover, answers buffered in the browser |
| **RPO ≤ 5 min / RTO ≤ 1 h** | Synchronous standby (no loss on AZ failure), point-in-time restore from 5-minute transaction logs, automatic failover in 1–2 min |
| **Records kept 5 years** | S3 lifecycle (IA → Glacier IR → Deep Archive → delete at 1,830 d), AWS Backup monthly snapshots kept 1,830 d, CloudTrail 5 y |
| **Encryption in transit and at rest** | TLS everywhere (CloudFront, API, `rds.force_ssl`, proxy `require_tls`, S3 deny non-TLS); KMS key for RDS, secrets, uploads, backups |

## Deliverables

| Brief | Location |
|---|---|
| README: summary, deploy and tear-down | this file |
| Task 1: architecture diagram | [architecture/architecture.png](architecture/architecture.png) |
| Task 1: two request walkthroughs | [architecture/walkthroughs.md](architecture/walkthroughs.md) |
| Task 1: exam peak + AZ loss (≤ 300 words) | [architecture/peak-and-az-resilience.md](architecture/peak-and-az-resilience.md) |
| Task 2: service table | [docs/services.md](docs/services.md) |
| Task 3: Cloud Architecture Lab build | [architecture/cloud-lab-walkthrough-1-exam-autosave.json](architecture/cloud-lab-walkthrough-1-exam-autosave.json), [architecture/cloud-lab-walkthrough-2-upload-notify.json](architecture/cloud-lab-walkthrough-2-upload-notify.json) |
| Task 3: Cloud Architecture Lab evidence | [evidence/cloud-architecture-lab.png](evidence/cloud-architecture-lab.png) |
| AWS deployment, IaC and CI/CD | [infra/](infra/), [backend/](backend/), [frontend/](frontend/), [.github/workflows/deploy.yml](.github/workflows/deploy.yml) |

```
architecture/          diagram, walkthroughs, peak/AZ explanation, Cloud Architecture Lab files
docs/services.md       service table
evidence/              Cloud Architecture Lab screenshots
infra/                 Terraform (infra/bootstrap: one-time state bucket + GitHub OIDC role)
backend/               Lambda code (API, submission processor, marker, notifier, migrate) + tests
frontend/              single-page app (HTML/JS/CSS, no build step)
scripts/               build, deploy-frontend, migrate, smoke-test, create-demo-users, integration-test
.github/workflows/     CI/CD pipeline
```

## Cloud Architecture Lab

Both files contain the same full architecture (2 AZs, public / private-app / private-data subnets, every service).
They differ only in where the simulated request starts: the lab reads the start point from the file, so each
walkthrough has its own file.

1. Open the lab → **Export** → **Upload JSON** → choose the file → **OK** to replace the workspace → **Fit View**.
2. **Send Request**:
   - `cloud-lab-walkthrough-1-exam-autosave.json`: Student → Route 53 → CloudFront → WAF → API Gateway → Lambda (AZ-A) → RDS Proxy → RDS Multi-AZ → **HTTP 200**.
   - `cloud-lab-walkthrough-2-upload-notify.json`: Student → S3 uploads (presigned POST) → SQS submissions → Lambda workers process it → **HTTP 202**.
3. **Failure Lab** → **Simulate AZ-A Outage** → **Send Request** again. The AZ-A Lambda and NAT gateway fail, API Gateway
   routes to the Lambda in AZ-B, and both walkthroughs still succeed.
4. Screenshots are in [evidence/](evidence/). The architecture diagram is the lab's **Download PNG** export of this design.

## Deploy

### Prerequisites

- An AWS account where you can create IAM roles (not an AWS Academy Learner Lab, which blocks IAM role creation and OIDC)
- AWS CLI v2 signed in as an administrator, Terraform ≥ 1.10, Python 3.12+, a GitHub repository for this code

### 1. Bootstrap (once, from your laptop)

Creates the Terraform state bucket and the IAM role that GitHub Actions assumes through OIDC (no AWS keys stored in GitHub).

```bash
cd infra/bootstrap
terraform init
terraform apply -var 'github_repository=<your-github-user>/<repo-name>' -var "github_subject_prefix=$(gh api repos/<your-github-user>/<repo-name>/actions/oidc/customization/sub --jq .sub_claim_prefix)"
# If the account already has a GitHub OIDC provider: add -var create_oidc_provider=false
```

### 2. Configure GitHub

Repository → **Settings → Secrets and variables → Actions → Variables**:

| Variable | Example |
|---|---|
| `AWS_REGION` | `ap-southeast-2` |
| `AWS_DEPLOY_ROLE_ARN` | from the bootstrap output |
| `TF_STATE_BUCKET` | from the bootstrap output |
| `SES_SENDER_EMAIL` | `you@example.com` |
| `SES_VERIFIED_RECIPIENTS` | `tutor@example.com,student@example.com` (SES sandbox only sends to verified addresses) |
| `ALARM_EMAIL` | `you@example.com` |

Then **Settings → Environments → New environment → `production`**.

### 3. Push to `main`

```bash
git push origin main
```

The pipeline runs unit + integration tests → `terraform apply` → uploads the SPA to S3 and invalidates CloudFront →
migrates the database → smoke-tests through CloudFront. The first run takes about 20–25 minutes. The site URL is printed
in the run summary.

### 4. After the first deploy

1. Click the AWS verification emails: SES (sender and each recipient) and the SNS alarm subscription.
2. Create demo users and seed a demo exam and assignment:
   ```bash
   ./scripts/build.sh
   terraform -chdir=infra init -backend-config="bucket=<state bucket>" -backend-config="key=exam-platform/prod.tfstate" \
     -backend-config="region=ap-southeast-2" -backend-config="use_lockfile=true"
   ./scripts/create-demo-users.sh admin@example.com tutor@example.com student@example.com 'Demo-Passw0rd-2026'
   ```
3. Open the site and sign in. Admins can create further users in the UI.

## Tests

```bash
python3 -m pytest backend/tests -q     # unit tests
./scripts/integration-test.sh           # real Lambda handlers against a throwaway PostgreSQL
```

## Tear down

Do this as soon as you have finished testing. NAT gateways, RDS Multi-AZ and RDS Proxy bill by the hour (about US$7/day).

```bash
# Terraform reads the Lambda zips even when destroying, and uses the same remote state as the pipeline
./scripts/build.sh
terraform -chdir=infra init -backend-config="bucket=<state bucket>" -backend-config="key=exam-platform/prod.tfstate" \
  -backend-config="region=ap-southeast-2" -backend-config="use_lockfile=true"

# 1. Everything the application created (buckets, backup vault and log groups are emptied automatically)
terraform -chdir=infra destroy -var ses_sender_email=you@example.com
#    Lambda network interfaces can take 20-40 min to be released; if destroy times out, run it again.

# 2. The bootstrap stack (state bucket + GitHub OIDC role)
terraform -chdir=infra/bootstrap destroy -var 'github_repository=<your-github-user>/<repo-name>'
```

Afterwards check the AWS console: **VPC → NAT gateways**, **RDS → databases/snapshots** and **EC2 → Elastic IPs** should be empty.
Disable the GitHub workflow so a later push doesn't recreate the stack.
