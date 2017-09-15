# Step Functions and Batch

Working together in peace and harmony; wouldn't that be nice?  It's probably on Amazon's roadmap for these two products, which definitely seem to be 'new and shiny' and getting a lot of buzz.  But if you're like me, you need this integration *now*, not in a few months or years time.

## Building

```bash
ACCOUNT_ID="YOUR_ACCOUNT_ID"
$(aws ecr get-login --no-include-email --region us-east-1)
docker build -t sfn_batch -f docker/app.docker .
docker tag sfn_batch:latest ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/sfn_batch:latest
docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/sfn_batch:latest
```
