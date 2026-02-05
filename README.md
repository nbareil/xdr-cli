# xdr-cli

Interact with XDR using the command-line.

All platforms are supported: Linux, Mac, and Windows.


[[_TOC_]]

# Installation

```
$ git clone https://github.com/nbareil/xdr-cli.git
$ cd xdr-cli
$ sudo apt install python3-venv
$ python3 -m venv venv && source venv/bin/activate && pip install .
$ xdr-cli --help
```

# Getting started

xdrcli is designed to be used with many tenants at the same time, in order for you to juggle easily between customers. For each client, you will define an alias name and specify the full URL and the API key and Identifier.

First, you have to add a tenant to your configuration:

```
$ xdr-cli config tenant add demo
Base URL (without https://): your-tenant.xdr.us.paloaltonetworks.com
Create an Advanced API key manually on https://your-tenant.xdr.us.paloaltonetworks.com/configuration/api-keys then:

API Key Hash: xxx
API Key ID: 123
```

The Key ID can be found here:

![Screenshot of Cortex XDR's UI](_doc/api_key_id.webp)

It is recommended to configure it with an expiration time (2 months from now is generally enough). In terms of privilege, you should use the "*Privileged Responder*" role.

Now, you are good to go:

```
$ xdr-cli --profile demo incident list
```

# Shell completion

Bash:

```
eval "$(_XDR_CLI_COMPLETE=bash_source xdr-cli)"
```

Zsh:

```
eval "$(_XDR_CLI_COMPLETE=zsh_source xdr-cli)"
```

Alternative: generate a static script and source it:

```
$ xdr-cli completion bash > ~/.xdr-cli-completion.bash
$ xdr-cli completion zsh > ~/.xdr-cli-completion.zsh
```

# Examples
## Get information about an endpoint

```
% xdr-cli -p demo endpoint get bookwormvm
{
    "endpoint_id": "a3b8b9c6d48f4bc1bae2320bb3bda078",
    "endpoint_name": "Bookwormvm",
    "endpoint_type": "AGENT_TYPE_SERVER",
    "endpoint_status": "CONNECTED",
    "os_type": "AGENT_OS_LINUX",
    "os_version": "10.13.0",
    "ip": [
        "10.0.2.15"
    ],
    "ipv6": [
        "fec0::4b2:7a69:3bac:1c8e",
        "fec0::5054:ff:fe12:3456",
        "fec0::a0e3:78ce:18f:ef31",
        "fec0::a4db:4946:906f:c2a7"
    ],
    "public_ip": "203.0.113.10",
    "users": [
        "root"
    ],
    "domain": "localdomain",
    "alias": "",
    "first_seen": 1737714227084,
    "last_seen": 1737961790740,
    "content_version": "1630-10375",
    "installation_package": "linux amd64",
    "active_directory": [],
    "install_date": 1737714227112,
    "endpoint_version": "8.6.1.129181",
    "is_isolated": "AGENT_UNISOLATED",
    "isolated_date": null,
    "group_name": [],
    "operational_status": "PROTECTED",
    "operational_status_description": "[]",
    "operational_status_details": [],
    "scan_status": "SCAN_STATUS_NONE",
    "content_release_timestamp": 1737458418000,
    "last_content_update_time": 1737714629021,
    "operating_system": "Debian 10.13",
    "mac_address": [
        "52:54:00:12:34:56"
    ],
    "assigned_prevention_policy": "U42 Linux Policy (Report Only) (2)",
    "assigned_extensions_policy": "",
    "token_hash": null,
    "tags": {
        "server_tags": [],
        "endpoint_tags": []
    },
    "content_status": "UP_TO_DATE"
}
```

You can then be fancy with the JSON output, for example, to programmatically get the endpoint ID:

```
$ xdr-cli -p demo endpoint get bookwormvm | jq -r .endpoint_id
"a3b8b9c6d48f4bc1bae2320bb3bda078"
```

If you are filling in the SoD Systems' tab, you may want its IP address, domain, and OS:

```
$ xdr-cli -p demo endpoint get bookwormvm | jq  '.ip, .domain, .operating_system'
[
  "10.0.2.15"
]
"localdomain"
"Debian 10.13"
```

## Triage

List available triage presets:

```
$ xdr-cli -p demo triage presets
```

Start a triage on a host:

```
$ xdr-cli -p demo triage new HOSTNAME
```

Optionally specify a triage configuration UUID:

```
$ xdr-cli -p demo triage new HOSTNAME --collector-uuid <UUID>
```

Check status for a triage action ID:

```
$ xdr-cli -p demo triage status 123456789
```


## Get information about an IP address

```
$ xdr-cli -p sandbox-u42 endpoint who-has 10.0.2.15
bookwormvm
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

## Execute XQL commands

You can execute XQL queries from the command-line:

```
$ xdr-cli --profile demo xql "dataset = xdr_data| filter action_process_username ~= "root"| fields _time,action_process_image_name'
```

By default, the JSON results will be output directly on the standard output in order for you to be able to pipe it to JQ and perform some magic voodo.

But if you are more an Excel master, you may enjoy the `--excel` flag that directly converts the ouptut to a full blown XLSX sheet:

```
$ xql_query='dataset = xdr_data| filter action_process_username ~= "root"| fields _time,action_process_image_name'
$ xdr-cli --profile demo xql  --excel -o my_sheet.xlsx  "$xql_query"'
```

You can also specify the dates as parameters:

```
$ xdr-cli --profile=demo  xql "$xql_query" --begin-time "2023-09-01 00:00:00" --end-time "2024-09-30 11:00:00"
```

And you are not even required to type the XQL query in the parameter, if you don't specify one, xdr-cli will automatically open your favorite text editor. The query will be automatically executed when you exit it:

```
$ xdr-cli --profile=demo xlq
```

## Script execution

You can run execute Python scripts directly on a list of endpoint IDs:

```
$ echo 'print "Hello world"' >  hello.py
$ xdr-cli -p demo script run -e 3ab2dfe3ded3416fad12f1c638eddf8b -e 677161eee18749df90adf0783bda9a21  hello.py
{"action_id": 522, "status": 1, "endpoints_count": 2}
```

Then you can get its execution status using the provided Action ID:

```
% xdr-cli -p demo script status 522|jq .
{
  "general_status": "COMPLETED_SUCCESSFULLY",
  "endpoints_pending": 0,
  "endpoints_canceled": 0,
  "endpoints_in_progress": 0,
  "endpoints_timeout": 0,
  "endpoints_failed": 0,
  "endpoints_completed_successfully": 2,
  "endpoints_pending_abort": 0,
  "endpoints_aborted": 0,
  "endpoints_expired": 0
}
```

Finally, you can request the script results:

```
$ xdr-cli -p demo script results 522|jq .
{
  "script_name": "\"snippet script\"",
  "script_description": null,
  "script_parameters": [],
  "date_created": "2025-01-23 08:34:28",
  "scope": "\"pw-zzz\"and 2 other endpoint",
  "error_message": "",
  "results": [
    {
      "endpoint_name": "pw-yyy",
      "endpoint_ip_address": [
        "10.2.44.12"
      ],
      "endpoint_status": "STATUS_010_CONNECTED",
      "domain": "demo.com",
      "endpoint_id": "677161eee18749df90adf0783bda9a21",
      "execution_status": "COMPLETED_SUCCESSFULLY",
      "standard_output": "Hello world",
      "retrieved_files": 0,
      "failed_files": 0,
      "retention_date": null
    },
    {
      "endpoint_name": "pw-xxx",
      "endpoint_ip_address": [
        "10.2.45.12"
      ],
      "endpoint_status": "STATUS_010_CONNECTED",
      "domain": "demo.com",
      "endpoint_id": "3ab2dfe3ded3416fad12f1c638eddf8b",
      "execution_status": "COMPLETED_SUCCESSFULLY",
      "standard_output": "Hello world",
      "retrieved_files": 0,
      "failed_files": 0,
      "retention_date": null
    }
  ]
}
```

Or extract the output:

```
$ xdr-cli -p demo script results 522|jq \
    '.results[]
    | select(.execution_status == "COMPLETED_SUCCESSFULLY")
    | { "endpoint_name": .endpoint_name, "standard_output": .standard_output}'
    {
      "endpoint_name": "pw-xxx",
      "standard_output": "Hello world"
    }
    {
      "endpoint_name": "pw-yyy",
      "standard_output": "Hello world"
    }
```

## Yara memory scan

Perform a memory scan of all processes using a Yara ruleset.

```
$ xdr-cli -p demo script yara-memory-scan -e 3ab2dfe3ded3416fad12f1c638eddf8b -e 677161eee18749df90adf0783bda9a21  chisel.yara
{"action_id": 537, "status": 1, "endpoints_count": 2}

$ xdr-cli -p demo script results 537
...
```

You can also use the `--all` flag to perform the scan on all endpoints, please
note that in this scenario, you must select a platform:

```
$ xdr-cli -p demo script yara-memory-scan --all --platform windows  chisel.yara
...
```


## Closing all alerts related to a given Alert name

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
