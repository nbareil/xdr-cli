# xdrcli

Interact with XDR using the command-line.

All platforms are supported: Linux, Mac, and Windows.

## Installation

```
$ git clone https://github.com/nbareil/xdr-cli.git
$ cd xdrcli
$ python setup.py install
$ xdrcli --help
```

## Getting started

xdrcli is designed to be used with many tenants at the same time, in order for you to juggle easily between customers. For each client, you will define an alias name and specify the full URL and the API key and Identifier.

First, you have to add a tenant to your configuration:

```
$ xdrcli config tenant add demo
Base URL (without https://): your-tenant.xdr.us.paloaltonetworks.com
Create an Advanced API key manually on https://your-tenant.xdr.us.paloaltonetworks.com/configuration/api-keys then:

API Key Hash: xxx
API Key ID: 123
```

It is recommended to configure it with an expiration time.

Now, you are good to go:

```
$ xdrcli --profile demo alerts list
```
