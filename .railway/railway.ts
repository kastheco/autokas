import { readFileSync } from 'node:fs'
import { defineRailway, github, image, preserve, project, service, volume } from 'railway/iac'

export const dashboardImage = 'wiziscool/oh-my-cpa:v0.2.9'

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
pids=""
trap 'kill $pids 2>/dev/null || true; wait' EXIT
trap 'exit 143' TERM INT
/usr/sbin/nginx -c /CLIProxyAPI/nginx.conf -g 'daemon off;' &
pids="$!"
/CLIProxyAPI/CLIProxyAPI -config /CLIProxyAPI/config.yaml &
pids="$pids $!"
wait -n $pids
exit 1`
export const proxyStart = `/bin/bash -ec '${proxyScript.replaceAll("'", "'\\''")}'`

export default defineRailway((ctx) => {
  const auth = volume('cli-proxy-auth', { sizeMB: 500, region: 'sfo' })
  const proxy = service('cli-proxy', {
    source: github('kastheco/autokas', { branch: 'main' }),
    build: { builder: 'DOCKERFILE', dockerfilePath: '.railway/Dockerfile.proxy', watchPatterns: ['.railway/**'] },
    networking: {
      serviceDomains: { 'cli-proxy-production-533a.up.railway.app': { port: 8317 } },
    },
    start: proxyStart,
    healthcheck: '/healthz',
    healthcheckTimeout: 300,
    replicas: 1,
    env: {
      PORT: '8317',
      CLI_PROXY_API_KEY: preserve(),
      MANAGEMENT_PASSWORD: preserve(),
    },
    volumeMounts: { '/data/auths': auth },
  })

  const dashboardData = volume('oh-my-cpa-data', { sizeMB: 1024, region: 'sfo' })
  const dashboard = service('oh-my-cpa', {
    source: image(dashboardImage),
    healthcheck: '/omc/api/healthz',
    env: {
      PORT: '8080',
      OMCPA_CPA_BASE_URL: 'http://cli-proxy.railway.internal:8318',
      OMCPA_PUBLIC_URL: 'https://${{RAILWAY_PUBLIC_DOMAIN}}',
      OMCPA_CPA_MANAGEMENT_KEY: proxy.env.MANAGEMENT_PASSWORD,
      OMCPA_MASTER_KEY: ctx.shared.OMCPA_MASTER_KEY,
      OMCPA_DATA_DIR: '/data',
      RAILWAY_RUN_UID: '0',
    },
    volumeMounts: { '/data': dashboardData },
  })

  return project('autokas', { resources: [proxy, auth, dashboard, dashboardData] })
})
