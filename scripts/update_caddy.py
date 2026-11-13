import os, pathlib, shutil, datetime
domain = os.environ['PAPER_DOMAIN']
token = os.environ['PAPER_TOKEN']
root = os.environ.get('PAPER_ROOT', '/srv/paper') + '/' + token
p = pathlib.Path('/home/ubuntu/new-api/Caddyfile')
s = p.read_text()
start = s.find('# >>> paper-trader')
if start != -1:
    end = s.find('# <<< paper-trader', start)
    s = s[:start] + s[end + len('# <<< paper-trader'):].lstrip('\n')
block = f'''# >>> paper-trader (managed by dsh-superagent ticket fe9b2dd99152)
{domain} {{
    tls {{
        dns cloudflare {{env.CF_API_TOKEN}}
    }}

    handle /{token} {{
        redir /{token}/ 308
    }}

    handle_path /{token}/* {{
        root * {root}
        encode gzip
        file_server
        header {{
            Cache-Control "no-store"
            X-Robots-Tag "noindex, nofollow"
            X-Content-Type-Options "nosniff"
            Referrer-Policy "no-referrer"
        }}
    }}

    handle {{
        respond 404
    }}

    log {{
        output file /var/log/caddy/access.log
    }}
}}
# <<< paper-trader
'''
backup = p.with_suffix('.bak-paper-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S'))
shutil.copy2(p, backup)
p.write_text(s.rstrip() + '\n\n' + block)
print('site block ensured for', domain)
