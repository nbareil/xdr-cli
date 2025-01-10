# xdrcli

Interact with XDR using the command-line.

All platforms are supported: Linux, Mac, and Windows.


[[_TOC_]]

## Installation

```
$ git clone https://github.com/nbareil/xdr-cli.git
$ cd xdr-cli
$ pip install .
$ xdr-cli --help
```

## Getting started

xdrcli is designed to be used with many tenants at the same time, in order for you to juggle easily between customers. For each client, you will define an alias name and specify the full URL and the API key and Identifier.

First, you have to add a tenant to your configuration:

```
$ xdr-cli config tenant add demo
Base URL (without https://): your-tenant.xdr.us.paloaltonetworks.com
Create an Advanced API key manually on https://your-tenant.xdr.us.paloaltonetworks.com/configuration/api-keys then:

API Key Hash: xxx
API Key ID: 123
```

It is recommended to configure it with an expiration time.

Now, you are good to go:

```
$ xdr-cli --profile demo incident list
```

## Examples

### Closing all alerts related to a given Alert name

In the following snippet, in the context of a CTF, we wanted to remove the 50k alerts that were raised quite rightly by Cortex XDR about CobaltStrike BEACON traffic to makes things easier for the students:

With the XDR logic, we have to:
1. List the incidents matching the noisy alert
2. For each incident, list of alerts that were raised
3. For each alert, close it
4. Finally, there is no alert remaining linked to the incident, close the whole incident

This gives the following loop:

```
incidents=$(xdr-cli --profile ctf incident list| jq 'select(.description|contains("Possible Beacon Traffic Detection")) | .incident_id' | sed 's/"//g')
for incident_id in $incidents; do
    alerts=$(xdr-cli --profile ctf incident get "$incident_id" | jq '.alerts.data[].alert_id'| sed 's/"//g')
    echo $alerts
    for alert_id in $alerts; do
            xdr-cli --profile ctf alert close "$alert_id"
    done
    xdr-cli --profile ctf incident close "$incident_id"
done
```

## Get a list of hosts matching conditions

List Linux hosts:

```
$ xdr-cli --profile demo endpoint list --platform linux|jq .endpoint_name|wc -l
2
```

List hosts having the tag XXX:

```
$ xdr-cli --profile demo endpoint list| jq 'select(.tag|contains("XXX")) | .endpoint_name'
```

## Acquire a file on host

```
$ xdr-cli --profile=demo get-file --hostname DESKTOP-V24D23 'C:\Temp\NetMonitor.exe' -o /tmp/net_monitor.zip
```

If you are going to acquire a large number of files, a slight optimization is to specify the *Endpoint ID* instead of the hostname to remove one back-and-forth with the server:

```
$ xdr-cli --profile=demo get-file --endpoint-id 1bd16395ef5a44a1db13033e461d012a /usr/local/bin/evil_service -o /tmp/evil_service.zip
```

## Bulk acquisition of files


Let's say you need to acquire a specific file on all Linux hosts, use the following loop:

```
$ for id in $(xdr-cli --profile demo endpoint list --platform linux|jq .endpoint_id); do
  xdr-cli --profile demo get-file --endpoint-id "$id" '/root/.bash_history' -o /tmp/collections/
done
```
