# Cognigy Log Poller — AWS Lambda to CloudWatch

This AWS Lambda function periodically fetches logs from the Cognigy.AI trial API and pushes them into AWS CloudWatch Logs for centralized monitoring and analysis.

It is designed for low-latency operation, minimal AWS API calls, and efficient batching with retry logic.

> ⚠️ **Warning — Demo Use Only**
>
> This Lambda is intended **solely for demonstration and educational purposes**.  
> It is **not optimized, secured, or tested for production workloads**.  
> Use at your own risk and do **not deploy to production environments** without appropriate modifications, security reviews, and testing.

## Overview

This Lambda automates log collection from Cognigy.AI - a conversational AI platform - allowing you to centralize, search, and monitor logs in AWS CloudWatch.  

It automatically persists state across invocations using AWS Systems Manager (SSM) Parameter Store, tracking:

- The timestamp of the last processed log (/cognigy-last-ts-ms)
- The CloudWatch sequence token (/cognigy-last-seq-token)
- It can use either an environment variable or a secure SSM parameter to store your Cognigy API key.

## AWS Resources Required
These resources should be created before running the Lambda function

| Resource                    | Purpose                                            | Example Name                                                           |
| --------------------------- | -------------------------------------------------- | ---------------------------------------------------------------------- |
| Lambda (python 3.10+)       | Executes the poller                                | cognigy-log-poller                                                     |
| CloudWatch Log Group        | Target for incoming Cognigy logs                   | cognigy-logs                                                           |
| CloudWatch Log Stream       | Dedicated stream for this Lambda                   | lambda-poller-stream                                                   |
| SSM Parameter Store         | Stores API key, last timestamp, and sequence token | /cognigy-mytrial-api-key, /cognigy-last-ts-ms, /cognigy-last-seq-token |
| Amazon EventBridge Schedule | Runs the Lambda poller every 1 minute              | EveryMinute                                                            |

		
## Configuration

### Environment Variables

| Variable        | Required | Description                                               |
| --------------- | -------- | --------------------------------------------------------- |
| COGNIGY_API_KEY | Optional | Cognigy API key (overrides SSM)                           |
| AWS_REGION      | Yes      | AWS region for SSM and CloudWatch (default: eu-central-1) |

### SSM Parameters

| Parameter                | Type         | Purpose                                           |
| ------------------------ | ------------ | ------------------------------------------------- |
| /cognigy-mytrial-api-key | SecureString | Cognigy API key (if not provided as env var)      |
| /cognigy-last-ts-ms      | String       | Last processed timestamp in milliseconds          |
| /cognigy-last-seq-token  | String       | CloudWatch sequence token (optional optimization) |
### How It Works

1. Initialization
	- Loads Cognigy API key from env var or SSM.
	- Reads last processed timestamp and (optional) CloudWatch sequence token.
2. Fetch from Cognigy
	- Calls https://api-trial.cognigy.ai/new/v2.0/projects/{PROJECT_ID}/logs
	- Uses pagination (next cursor) to fetch all new logs newer than the last timestamp.
3. Prepare Log Events
	- Converts ISO timestamps to epoch milliseconds.
	- Compresses each log entry to a single-line JSON message.
4. Send to CloudWatch
	- Writes logs to the configured group and stream in chronological order.
	- Retries on InvalidSequenceTokenException or missing stream creation.
5. Persist State
	- Updates the last timestamp and sequence token in SSM.
