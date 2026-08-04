---
title: "Live events: Microsoft 365"
description: "Near-real-time SharePoint and OneDrive change delivery through Azure Event Hubs: outbound-only, no inbound webhook."
---

This is the customer-admin setup for near-real-time SharePoint and OneDrive
sync on an on-prem appliance. Microsoft Graph publishes a small change
notification to the customer's Event Hub; the appliance consumes it
**outbound-only** and runs the normal delta feed. No public webhook or inbound
firewall rule is required.

One setup serves both connectors: OneDrive drives and SharePoint document
libraries use the same `/drives/{id}/root` subscription resource, and the
appliance runs one shared Event Hubs consumer for the whole Microsoft Graph
family. The scheduled incremental sync remains the reconciliation fallback if
an event is delayed or missed.

## What the customer must provide

- An Azure subscription in the same Entra tenant as SharePoint.
- Permission to create Event Hubs resources and Azure role assignments
  (`Owner` or `User Access Administrator` plus resource creation is enough).
- The appliance's Entra application. A dedicated Event Hubs receiver
  application also needs a client secret kept in the customer's secret
  manager.
- A SharePoint connection whose first sync has discovered its document
  libraries.

Use browser-based Azure CLI login:

```bash
az login --tenant CUSTOMER_TENANT_ID
```

Do not use `--use-device-code` when Entra Security Defaults blocks the
device-code flow (Microsoft enabled that block for new tenants beginning
July 1, 2026), and do not disable Security Defaults to make this setup work.

## Create the Event Hub

Choose customer-specific names:

```bash
SUBSCRIPTION_ID="00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP="legalmemory"
LOCATION="westeurope"
NAMESPACE="lm-graph-events-customer"
EVENT_HUB="legalmemory"
CONSUMER_GROUP='$Default'

az account set --subscription "$SUBSCRIPTION_ID"
az provider register --namespace Microsoft.EventHub --wait
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"
az eventhubs namespace create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$NAMESPACE" \
  --location "$LOCATION" \
  --sku Basic \
  --capacity 1 \
  --minimum-tls-version 1.2
az eventhubs eventhub create \
  --resource-group "$RESOURCE_GROUP" \
  --namespace-name "$NAMESPACE" \
  --name "$EVENT_HUB" \
  --partition-count 2 \
  --retention-time-in-hours 24 \
  --cleanup-policy Delete
```

Basic namespaces use the built-in `$Default` consumer group. If the hub is
shared with another consumer, use Standard tier and create a dedicated group:

```bash
az eventhubs eventhub consumer-group create \
  --resource-group "$RESOURCE_GROUP" \
  --namespace-name "$NAMESPACE" \
  --eventhub-name "$EVENT_HUB" \
  --name legalmemory
```

## Grant the two Event Hubs roles

There are two different identities, and neither role should be granted at
subscription scope:

1. **Microsoft Graph Change Tracking** publishes notifications and needs
   **Azure Event Hubs Data Sender** on this event hub.
2. The appliance's Entra application consumes notifications and needs
   **Azure Event Hubs Data Receiver** on this event hub.

In the Azure portal: *Event Hubs namespace → Event Hubs → &lt;hub&gt; → Access
control (IAM)*, add the two role assignments. The equivalent CLI:

```bash
EVENT_HUB_ID="$(az eventhubs eventhub show \
  --resource-group "$RESOURCE_GROUP" \
  --namespace-name "$NAMESPACE" \
  --name "$EVENT_HUB" \
  --query id -o tsv)"

GRAPH_CHANGE_TRACKING_OBJECT_ID="$(az ad sp list \
  --filter \"appId eq '0bf30f3b-4a52-48df-9a82-234910c4a086'\" \
  --query '[0].id' -o tsv)"
LM_APP_OBJECT_ID="$(az ad sp show \
  --id CUSTOMER_APPLIANCE_CLIENT_ID \
  --query id -o tsv)"

az role assignment create \
  --assignee-object-id "$GRAPH_CHANGE_TRACKING_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Event Hubs Data Sender" \
  --scope "$EVENT_HUB_ID"
az role assignment create \
  --assignee-object-id "$LM_APP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Event Hubs Data Receiver" \
  --scope "$EVENT_HUB_ID"
```

If `Microsoft Graph Change Tracking` is missing, a tenant administrator must
create its service principal using the globally fixed application id
`0bf30f3b-4a52-48df-9a82-234910c4a086`. Microsoft documents both the lookup
and the creation request in
[Receive change notifications through Azure Event Hubs](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-event-hubs#what-if-the-microsoft-graph-change-tracking-application-is-missing).

## Configure the appliance

Store the receiver secret in the customer's secret manager and provide these
deployment values **to the `app` container only**, never to the worker or
watcher containers:

```dotenv
KI_MICROSOFT_EVENTS_NOTIFICATION_URL=EventHub:https://lm-graph-events-customer.servicebus.windows.net/eventhubname/legalmemory?tenantId=customer.onmicrosoft.com
KI_MICROSOFT_EVENTS_NAMESPACE=lm-graph-events-customer.servicebus.windows.net
KI_MICROSOFT_EVENTS_EVENT_HUB=legalmemory
KI_MICROSOFT_EVENTS_CONSUMER_GROUP='$Default'
KI_MICROSOFT_EVENTS_TENANT_ID=00000000-0000-0000-0000-000000000000
KI_MICROSOFT_EVENTS_CLIENT_ID=00000000-0000-0000-0000-000000000000
# Required for a dedicated receiver application. If CLIENT_ID is the same
# application used by an active SharePoint connection, the appliance reuses
# that connection's encrypted client secret and this may stay empty.
KI_MICROSOFT_EVENTS_CLIENT_SECRET=
```

The notification URL (the `EventHub:` form Microsoft Graph writes to) is
deliberately a separate setting from the AMQP coordinates the appliance
consumes from; keeping them apart prevents a portal value being pasted into
the wrong field and producing a subscription that can never be received.

Restart the `app` container. After the first SharePoint sync, the appliance
creates one Graph subscription per discovered document library and renews it
three days before its 29-day expiry. Event Hubs partition offsets are stored
in Postgres, so restarts resume from the retained broker position.

## Verify

```bash
docker compose logs app | rg "Microsoft Event Hubs consumer started"
```

```sql
SELECT status, target, external_id, expires_at, last_event_at, last_error
FROM connector_event_subscriptions
WHERE adapter = 'microsoft_graph_sharepoint';
```

Then upload one harmless file and confirm: `last_event_at` changes, an
event-triggered `source-sync` run starts, the file is inserted exactly once,
and deleting it produces a tombstone without starting an unrelated insertion
batch.
