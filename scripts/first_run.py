"""Local setup guidance; no downloads, grading, credentials or config mutations."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import threading


def serve_setup(port, check):
    token = secrets.token_urlsafe(32)
    origin = f'http://127.0.0.1:{port}'
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>墨评 · 准备本机评分</title>
<style>body{margin:0;background:#f3f0e9;color:#263b35;font:16px/1.7 system-ui}main{max-width:760px;margin:8vh auto;padding:28px}small{color:#62736d;letter-spacing:.12em}h1{font-size:clamp(28px,5vw,42px);line-height:1.3}article{background:#fffdf8;border:1px solid #d9dfd4;border-radius:18px;padding:24px;margin:24px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#e9eee5;padding:18px;border-radius:10px;font-size:14px}button{font:inherit;border:0;border-radius:10px;background:#254d41;color:white;padding:12px 20px;min-height:48px;cursor:pointer}button:hover{background:#183c30}a:hover{text-decoration-thickness:2px}button:disabled{opacity:.6}a{color:#286751}p{max-width:65ch}li{margin:12px 0}:focus-visible{outline:3px solid #b77732;outline-offset:4px}@media(max-width:480px){main{padding:20px;margin:3vh auto}article{padding:18px}}</style>
<a href="#main" style="position:absolute;left:20px;top:10px">跳到主要内容</a><main id="main"><small>墨评 / 本机工作区</small><h1>先准备评分模型</h1><p>程序与练习规则已经就位。本机评分服务尚未就绪；现有作业和报告会保留，不会生成替代分数。</p>
<article><h2>首次使用</h2><ol><li>从 <a href="https://ollama.com/download" target="_blank" rel="noreferrer">Ollama 官网</a>安装并打开本机服务。选择本地模型，无需云端密钥。</li><li>在终端准备模型（需要网络、磁盘与内存；已安装的模型无需重下）：</li></ol><pre>ollama pull deepseek-r1:7b
ollama pull bge-m3
printf 'FROM deepseek-r1:7b\nPARAMETER num_ctx 65536\nPARAMETER num_predict 4096\nPARAMETER temperature 0\n' &gt; /tmp/inkwise-local.Modelfile
ollama create inkwise-deepseek-local -f /tmp/inkwise-local.Modelfile</pre><p>已有其他服务配置时，按使用说明核对私人工作区中的配置；不要上传配置或教师访问文件。</p><button id="retry">重新检查并打开工作区</button><p id="status" role="status" aria-live="polite">此检查只查询模型目录，不执行评分。</p></article><p>进入后使用私人工作区 teacher-access.txt 中的教师凭据登录。13 套初始量规为练习草案，正式教学前需学校审核。</p></main>
<script nonce="TOKEN">document.querySelector('#retry').onclick=async()=>{const b=document.querySelector('#retry'),s=document.querySelector('#status');b.disabled=true;s.textContent='正在检查本机服务…';try{const r=await fetch('/setup/check',{method:'POST',headers:{'X-Setup-Token':'TOKEN'}});const v=await r.json();if(v.ready){s.textContent='模型目录就绪，正在打开工作区…';let opened=false;for(let i=0;i<40;i++){await new Promise(r=>setTimeout(r,500));try{const h=await fetch('/health/ready');if(h.ok&&(await h.json()).status==='ready'){opened=true;location.reload();break;}}catch{}}if(!opened)s.textContent='工作区仍在启动，请稍后重新载入页面。';}else{s.textContent='尚未就绪：'+v.failed.join('、')+'。请核对模型名称与服务，再重新检查。';b.disabled=false;}}catch{s.textContent='连接暂未恢复，请稍后重新检查。';b.disabled=false;}};</script></html>'''.replace('TOKEN', token).encode()
    ready = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, kind):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{token}'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != origin.removeprefix('http://'):
                return self.send(403, b'Forbidden', 'text/plain')
            if self.path not in {'/', '/workspace/'}:
                return self.send(404, b'Not found', 'text/plain')
            self.send(200, page, 'text/html; charset=utf-8')

        def do_POST(self):
            nonlocal ready
            if (self.path != '/setup/check' or self.headers.get('Host') != origin.removeprefix('http://')
                    or self.headers.get('Origin') != origin
                    or not secrets.compare_digest(self.headers.get('X-Setup-Token', ''), token)
                    or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0'):
                return self.send(403, b'Forbidden', 'text/plain')
            report = check()
            ready = report['ready']
            body = json.dumps({'ready': ready, 'failed': [row['name'] for row in report['checks'] if row['status'] == 'fail']}).encode()
            self.send(200, body, 'application/json')
            if ready:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    class Server(HTTPServer):
        allow_reuse_address = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    with Server(('127.0.0.1', port), Handler) as server:
        server.serve_forever(poll_interval=.2)
    return ready
