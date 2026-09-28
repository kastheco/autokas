"""Serialize deployments in Actions and persist the intake pause across failures."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import modal
import runner

APP = runner.CONFIG['app']
URL = runner.CONFIG['docs_update']['webhook_url']
STATE_KEY = '__deployment_pause__'


def github(path: str, method: str = 'GET', data: Any = None) -> Any:
    """Use gh's environment credential without logging credentials or responses."""
    args = ['gh', 'api', '--method', method, path]
    if data is not None:
        args += ['--input', '-']
    for attempt in range(3):
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True)
        if result.returncode == 0:
            return json.loads(result.stdout) if result.stdout else None
        if method not in {'GET', 'PATCH'} or attempt == 2:
            raise RuntimeError(f'GitHub {method} failed: {path}')
        time.sleep(2 ** attempt)


def pages(path: str) -> list[dict[str, Any]]:
    result = []
    for page in range(1, 10000):
        batch = github(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('GitHub pagination exceeded limit')


def save(state: dict[str, Any]) -> None:
    runner.CLAIMS.put(STATE_KEY, state)


def discover() -> dict[str, Any]:
    hooks = []
    for repo in pages('user/repos?affiliation=owner,collaborator,organization_member'):
        if not repo.get('permissions', {}).get('admin'):
            continue
        name = repo['full_name']
        for hook in pages(f'repos/{name}/hooks'):
            if hook.get('active') and hook.get('config', {}).get('url') == URL:
                hooks.append({'repo': name, 'id': hook['id']})
    if not hooks:
        raise RuntimeError('No active runner hooks found; refusing an unpaused deployment')
    return {'started': (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
            'hooks': hooks, 'restored': False, 'reconciled': [], 'revision': runner.REVISION}


def set_hook(hook: dict[str, Any], active: bool) -> None:
    path = f"repos/{hook['repo']}/hooks/{hook['id']}"
    current = github(path)
    if current.get('config', {}).get('url') != URL:
        raise RuntimeError(f'Hook target changed: {path}')
    if current['active'] != active:
        github(path, 'PATCH', {'active': active})
    if github(path)['active'] != active:
        raise RuntimeError(f'Hook state not confirmed: {path}')


def drain() -> None:
    functions = [modal.Function.from_name(APP, name) for name in ('webhook', 'worker')]
    functions.append(modal.Cls.from_name(APP, 'PRWorker')._get_class_service_function())
    deadline = time.monotonic() + 4500
    quiet = 0
    while time.monotonic() < deadline:
        stats = [fn.get_current_stats() for fn in functions]
        idle = all(s.backlog == 0 and s.num_running_inputs == 0 for s in stats)
        quiet = quiet + 1 if idle else 0
        if quiet >= 3:
            print('receiver, dispatcher and all PR pools drained', flush=True)
            return
        time.sleep(10)
    raise TimeoutError('Workers did not drain; deployment was not started')


def verify(state: dict[str, Any]) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(urllib.request.Request(URL, data=b'{}'), timeout=40).close()
        except urllib.error.HTTPError as error:
            if error.code == 401:
                break
        except (OSError, TimeoutError):
            pass
        time.sleep(5)
    else:
        raise RuntimeError('Receiver did not reject unsigned requests with 401')
    hook = next(h for h in state['hooks'] if h['repo'] == 'example-org/example-app')
    path = f"repos/{hook['repo']}/hooks/{hook['id']}"
    before = {d['id'] for d in github(path + '/deliveries?per_page=100')}
    github(path + '/pings', 'POST')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        for delivery in github(path + '/deliveries?per_page=100'):
            if delivery['id'] not in before and delivery['event'] == 'ping' and delivery['status_code'] == 200:
                receipt = github(path + f"/deliveries/{delivery['id']}")
                body = json.loads(receipt['response']['payload'])
                if body.get('revision') != runner.REVISION:
                    raise RuntimeError('Signed ping returned the wrong deployed revision')
                print(f"signed GitHub ping verified: {delivery['id']}", flush=True)
                return
        time.sleep(5)
    raise RuntimeError('Signed GitHub ping did not return 200')


def reconcile_repo(repo: str, since: str) -> None:
    """Rebuild only eligible events, retaining the receiver's atomic deduplication."""
    worker = modal.Function.from_name(APP, 'worker')

    def dispatch(event: str, payload: dict[str, Any]) -> None:
        job = runner.event_job(event, payload)
        if job and runner.CLAIMS.put(job['key'], 'claimed', skip_if_exists=True):
            # An uncertain spawn must never be automatically replayed.
            call = worker.spawn(job)
            print(json.dumps({'reconciled_job': job['key'], 'call_id': call.object_id}), flush=True)

    for issue in pages(f'repos/{repo}/issues?state=all&since={since}'):
        if 'pull_request' not in issue:
            continue
        number = issue['number']
        pr = github(f'repos/{repo}/pulls/{number}')
        base = {'repository': {'full_name': repo}, 'pull_request': pr}
        if pr.get('merged_at') and pr['merged_at'] >= since:
            dispatch('pull_request', {**base, 'action': 'closed'})
        if pr['state'] != 'open':
            continue
        for kind, path, key in [
            ('issue_comment', f'issues/{number}/comments?since={since}', 'comment'),
            ('pull_request_review_comment', f'pulls/{number}/comments', 'comment'),
            ('pull_request_review', f'pulls/{number}/reviews', 'review'),
        ]:
            for item in pages(f'repos/{repo}/{path}'):
                # Reviews have no updated_at. Include old reviews on a changed PR
                # so edits are not lost; existing fingerprints deduplicate them.
                if kind != 'pull_request_review' and item.get('updated_at', '') < since:
                    continue
                dispatch(kind, {**base, 'issue': issue, key: item, 'sender': item['user'],
                                'action': 'submitted' if key == 'review' else 'created'})


def restore_and_reconcile(state: dict[str, Any]) -> None:
    errors = []
    for hook in state['hooks']:
        try:
            set_hook(hook, True)
        except Exception as error:
            errors.append(error)
    if errors:
        raise RuntimeError(f'{len(errors)} hooks could not be restored') from errors[0]
    state['restored'] = True
    save(state)
    print(f"all {len(state['hooks'])} hooks restored", flush=True)
    for repo in sorted({h['repo'] for h in state['hooks']}):
        if repo not in state['reconciled']:
            reconcile_repo(repo, state['started'])
            state['reconciled'].append(repo)
            save(state)
    state['complete'] = True
    save(state)
    print(f"all {len(state['reconciled'])} repositories reconciled", flush=True)


def main() -> None:
    previous = runner.CLAIMS.get(STATE_KEY, None)
    if os.environ.get('DEPLOY_CLEANUP') == '1':
        if previous and not previous.get('complete'):
            restore_and_reconcile(previous)
        return
    if previous and not previous.get('complete'):
        restore_and_reconcile(previous)
    state = discover()
    # Persist the complete restoration list BEFORE the first external mutation.
    save(state)
    try:
        for hook in state['hooks']:
            set_hook(hook, False)
        print(f"paused {len(state['hooks'])} hooks", flush=True)
        drain()
        subprocess.run(['modal', 'deploy', '--strategy', 'recreate', 'runner.py'], check=True)
        verify(state)
        print(f'deployed revision: {runner.REVISION}', flush=True)
    finally:
        restore_and_reconcile(state)


if __name__ == '__main__':
    main()
