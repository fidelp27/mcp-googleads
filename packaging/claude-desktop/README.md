# Google Ads MCP for Claude Desktop

Install `google-ads-mcp.mcpb` under Claude Desktop → Settings → Desktop app → Extensions → Advanced settings → Install Extension. Claude Desktop manages the Python environment using uv. The first startup requires a network connection to download dependencies.

The default mode is `demo`. Ask Claude to list the demo accounts and show campaign performance for one of them. The fixtures are synthetic.

To connect a real account, install the Google Ads MCP CLI from the same source version and run:

```sh
google-ads-mcp-server connect --client generic --oauth-client /path/to/client_secret.json
```

Use your own Google Cloud project and Google Ads access. See the project [connection guide](https://github.com/fidelp27/mcp-googleads) for account selection and the OAuth setup. After authorizing, select `account` in the extension's settings. The extension uses the default local profile unless you select a different `profile.json`. Keep the profile private and use a consistent absolute path if you override it.

Both modes expose the read-only `core` catalog. Real mode scopes requests to the accounts authorized in the profile. Claude receives the reports and account data you ask it to retrieve.
