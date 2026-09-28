"""Repository installation isolation and refresh regressions."""
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import runner


class InstallationTests(unittest.TestCase):
    def test_repository_requests_never_reuse_another_installation_token(self):
        calls = []
        now = [1000]
        installations = {'example-owner-3/example-repo-3': 21, 'example-org/example-app': 42}

        def serve(request, timeout):
            path = request.full_url.removeprefix('https://api.github.com/')
            calls.append(path)
            if path.endswith('/installation'):
                repo = path.removeprefix('repos/').removesuffix('/installation')
                if repo not in installations:
                    raise HTTPError(request.full_url, 404, 'Not Found', {}, None)
                self.assertEqual(request.get_header('Authorization'), 'Bearer app-jwt')
                value = {'id': installations[repo]}
            elif path.startswith('app/installations/'):
                value = {'token': 'token-' + path.split('/')[2]}
            else:
                repo = '/'.join(path.split('/')[1:3]).lower()
                self.assertEqual(request.get_header('Authorization'), f'Bearer token-{installations[repo]}')
                value = {'number': 289}
            return io.BytesIO(json.dumps(value).encode())

        with patch.dict(runner._github_tokens, {}, clear=True), patch.dict(os.environ, GITHUB_APP_ID='1', GITHUB_APP_PRIVATE_KEY='fixture'), patch('jwt.encode', return_value='app-jwt'), patch.object(runner.time, 'time', side_effect=lambda: now[0]), patch.object(runner.urllib.request, 'urlopen', side_effect=serve):
            runner.github('repos/example-owner-3/example-repo-3/pulls/289')
            runner.github('repos/example-org/example-app/pulls/1')
            runner.github('repos/example-owner-3/example-repo-3/pulls/290')
            self.assertEqual(calls.count('app/installations/21/access_tokens'), 1)
            self.assertEqual(calls.count('app/installations/42/access_tokens'), 1)
            now[0] += 3600
            runner.github('repos/example-owner-3/example-repo-3/pulls/292')
            self.assertEqual(calls.count('app/installations/21/access_tokens'), 2)
            with self.assertRaises(HTTPError):
                runner.github('repos/not-installed/private/pulls/1')
            self.assertNotIn('repos/not-installed/private/pulls/1', calls)


if __name__ == '__main__':
    unittest.main()
