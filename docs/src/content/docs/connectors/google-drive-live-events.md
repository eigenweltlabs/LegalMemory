---
title: "Live events: Google Drive"
description: "Near-real-time Drive change delivery through Google Workspace Events and Pub/Sub: outbound-only pull."
---

Drive event delivery is **outbound-only**: Google publishes to a Pub/Sub topic
in the customer's project and the appliance pulls from a subscription on it.
No inbound webhook is required. The connection's continuous interval remains
enabled as reconciliation: it catches a lost or expired subscription, a
broker outage, and the periodic full ACL refresh.

The topic must be in the **same Google Cloud project as the Drive OAuth
client**. The existing read-only Drive OAuth scopes authorize the user-level
Workspace Events subscriptions; the service account below is only the Pub/Sub
consumer.

## Set up Pub/Sub

1. Enable **Google Drive API**, **Admin SDK API**, **Google Workspace Events
   API**, and **Cloud Pub/Sub API**.
2. Create a Pub/Sub topic and a pull subscription on that topic.
3. On the topic, grant **Pub/Sub Publisher** to
   `drive-api-event-push@system.gserviceaccount.com`.
4. Create a tenant-local service account for the appliance and grant it
   **Pub/Sub Subscriber** on the pull subscription.

Reproducible `gcloud` setup (use the project that owns the Drive OAuth
client):

```bash
PROJECT=customer-oauth-project
TOPIC=legalmemory-drive
SUBSCRIPTION=legalmemory-drive
SERVICE_ACCOUNT=legalmemory-events

gcloud services enable drive.googleapis.com admin.googleapis.com \
  workspaceevents.googleapis.com pubsub.googleapis.com --project "$PROJECT"
gcloud pubsub topics create "$TOPIC" --project "$PROJECT"
gcloud pubsub subscriptions create "$SUBSCRIPTION" \
  --topic "$TOPIC" --project "$PROJECT"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member serviceAccount:drive-api-event-push@system.gserviceaccount.com \
  --role roles/pubsub.publisher --project "$PROJECT"
gcloud iam service-accounts create "$SERVICE_ACCOUNT" --project "$PROJECT"
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
  --member "serviceAccount:${SERVICE_ACCOUNT}@${PROJECT}.iam.gserviceaccount.com" \
  --role roles/pubsub.subscriber --project "$PROJECT"
```

Prefer workload identity where the platform offers it. For a standalone
on-prem host, create a key for that narrowly scoped service account and mount
it read-only; the key must never be placed in `config.json` or committed with
the appliance. The Compose deployment mounts only
`KI_CONNECTOR_EVENTS_SECRET_DIR` into the **app** container (at
`/run/connector-events`); workers that parse documents cannot read it.

## Configure the appliance

```dotenv
KI_GOOGLE_EVENTS_TOPIC=projects/CUSTOMER_PROJECT/topics/legalmemory-drive
KI_GOOGLE_EVENTS_PULL_SUBSCRIPTION=projects/CUSTOMER_PROJECT/subscriptions/legalmemory-drive
KI_CONNECTOR_EVENTS_SECRET_DIR=./runtime/secrets
KI_GOOGLE_EVENTS_SERVICE_ACCOUNT_FILE=/run/connector-events/google-events.json
# Alternative to the mounted file:
# KI_GOOGLE_EVENTS_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

Restart the app container.

## Behaviour and limits

- The appliance creates one no-resource-payload subscription for each selected
  Drive folder or shared drive (descendants included) and renews it before
  Google's seven-day maximum.
- Google does not allow a Workspace Events subscription on the whole
  **My Drive root**. A selected My Drive folder works; a whole-My-Drive
  connection is honestly shown as *reconciliation only* and still syncs
  incrementally through Drive Changes.
- Google currently rejects the advertised `file.v3.untrashed` event for Shared
  Drive folders; restored items are picked up by the reconciliation interval
  instead.
- Event bodies are never trusted as indexed state: a notification only wakes
  the connection's delta feed, and the normal content/metadata/ACL diff
  decides what changes.
