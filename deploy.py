"""Serialize deployments in Actions and reconcile every installed repository afterwards."""
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


def installed_repos() -> list[str]:
    """The App installations are the delivery list, so they are the reconcile list."""
    return modal.Function.from_name(APP, 'installed_repos').remote()


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


def verify() -> None:
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
    # The App key stays in Modal, so the signed replay runs there too.
    receipt = modal.Function.from_name(APP, 'redeliver_latest').remote()
    if receipt['status_code'] not in {200, 202}:
        raise RuntimeError(f"Signed App delivery returned {receipt['status_code']}")
    if json.loads(receipt['body']).get('revision') != runner.REVISION:
        raise RuntimeError('Signed App delivery returned the wrong deployed revision')
    print(f"signed App delivery verified: {receipt['id']}", flush=True)


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


def reconcile_all(state: dict[str, Any]) -> None:
    for repo in installed_repos():
        if repo not in state['reconciled']:
            reconcile_repo(repo, state['started'])
            state['reconciled'].append(repo)
            save(state)
    state['complete'] = True
    save(state)
    print(f"all {len(state['reconciled'])} repositories reconciled", flush=True)


def main() -> None:
    previous = runner.CLAIMS.get(STATE_KEY, None)
    unfinished = previous if previous and not previous.get('complete') else None
    if os.environ.get('DEPLOY_CLEANUP') == '1':
        if unfinished:
            reconcile_all(unfinished)
        return
    # An unfinished earlier reconcile is folded into this one by keeping its start.
    started = unfinished['started'] if unfinished else (
        datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    state = {'started': started, 'reconciled': [], 'revision': runner.REVISION}
    # Persist the reconcile window BEFORE the cutover can lose deliveries.
    save(state)
    try:
        drain()
        subprocess.run(['modal', 'deploy', '--strategy', 'recreate', 'runner.py'], check=True)
        verify()
        print(f'deployed revision: {runner.REVISION}', flush=True)
    finally:
        reconcile_all(state)


if __name__ == '__main__':
    main()
