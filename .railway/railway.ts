import { readFileSync } from 'node:fs'
import { defineRailway, image, preserve, project, service, volume } from 'railway/iac'

export const proxyImage = 'eceasy/cli-proxy-api:v8.0.27'

// Embed the YAML in the image's startup command, expanding only its API-key placeholder.
const proxyYaml = readFileSync(new URL('./proxy.yaml', import.meta.url), 'utf8')
const shellYaml = proxyYaml
  .replace(/[\\$`"]/g, '\\$&')
  .replaceAll('\\${CLI_PROXY_API_KEY}', '${CLI_PROXY_API_KEY}')
const proxyScript = `umask 077
: "\${CLI_PROXY_API_KEY:?CLI_PROXY_API_KEY is required}"
mkdir -p /data/auths
chmod 700 /data/auths
printf '%s' "${shellYaml}" > /CLIProxyAPI/config.yaml
exec /CLIProxyAPI/CLIProxyAPI -config /CLIProxyAPI/config.yaml`
export const proxyStart = `/bin/sh -ec '${proxyScript.replaceAll("'", "'\\''")}'`

export default defineRailway(() => {
  const auth = volume('cli-proxy-auth', { sizeMB: 500, region: 'sfo' })
  const proxy = service('cli-proxy', {
    source: image(proxyImage),
    start: proxyStart,
    healthcheck: '/healthz',
    healthcheckTimeout: 300,
    replicas: 1,
    env: {
      PORT: '8317',
      CLI_PROXY_API_KEY: preserve(),
    },
    volumeMounts: { '/data/auths': auth },
  })

  return project('autokas', { resources: [proxy, auth] })
})
