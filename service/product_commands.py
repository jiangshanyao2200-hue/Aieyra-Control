"""Fixed product actions via the existing bridge, using its durable ledger."""
import json
from pathlib import Path
import re
import subprocess
import sys
from os_sessions import bridge_config


class ProductCommandError(Exception):
    pass


class ProductCommands:
    def __init__(self, sources):
        self.source_provider = sources if callable(sources) else lambda: sources

    @property
    def sources(self):
        return {source['id']: source for source in self.source_provider()}

    def call(self, source_id, request):
        if not isinstance(source_id, str) or source_id not in self.sources:
            raise ProductCommandError('product_source_not_registered')
        source = self.sources[source_id]
        # Paths come exclusively from validated local configuration.
        script = Path(source['adapter_dir']) / 'bridge.py'
        try:
            bridge_config(source)
            arguments = [sys.executable, '-X', 'utf8', str(script), '--config', source['config_ref']]
            if request.get('operation') in ('human_sync', 'product_job_sync'):
                field = 'human_host_ref' if request['operation'] == 'human_sync' else 'product_host_ref'
                reference = source.get(field)
                if not isinstance(reference, str) or not Path(reference).is_absolute():
                    raise ProductCommandError(field.replace('_ref', '_not_configured'))
                arguments += ['--' + field.replace('_', '-'), reference]
            process = subprocess.run(arguments,
                                     input=json.dumps(request, ensure_ascii=False).encode('utf-8'),
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     timeout=75, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if len(process.stdout) > 32 * 1048576:
                raise ProductCommandError('product_output_limit_query_status')
            result = json.loads(process.stdout)
            if not isinstance(result, dict):
                raise ValueError('invalid_response')
            if process.returncode == 0 and result.get('ok') is True:
                return result['data']
            code = result.get('error')
            raise ProductCommandError(code if isinstance(code, str) and re.fullmatch('[a-z0-9_]{1,100}', code) else 'product_failed_query_status')
        except subprocess.TimeoutExpired:
            # The subprocess is ours; a submitted runtime command is not replayed.
            raise ProductCommandError('product_outcome_unknown_query_status') from None
        except (OSError, ValueError, KeyError):
            raise ProductCommandError('product_unavailable_query_status') from None

    def command(self, value):
        if set(value) != {'source_id', 'command'}:
            raise ProductCommandError('invalid_product_command')
        return self.call(value['source_id'], {'operation': 'command', 'command': value['command']})

    def dispatch(self, value):
        if set(value) != {'source_id', 'task'}:
            raise ProductCommandError('invalid_product_dispatch')
        return self.call(value['source_id'], {'operation': 'dispatch', 'task': value['task']})

    def status(self, source_id, request_id):
        return self.call(source_id, {'operation': 'command_status', 'request_id': request_id})

    def matrix_status(self, source_id):
        return self.call(source_id, {'operation': 'matrix_status'})

    def human_sync(self, source_id):
        return self.call(source_id, {'operation': 'human_sync'})

    def product_job_sync(self, source_id):
        return self.call(source_id, {'operation': 'product_job_sync'})
