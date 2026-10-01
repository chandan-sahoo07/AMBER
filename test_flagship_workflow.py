import urllib.request
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Authenticate
login_data = json.dumps({'email': 'sarah.connor@support-ai.enterprise', 'password': 'password123', 'role': 'ADMIN'}).encode('utf-8')
login_req = urllib.request.Request('http://localhost:8000/api/auth/login', data=login_data, headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(login_req, timeout=5) as login_res:
    token = json.loads(login_res.read().decode('utf-8'))['token']

headers = {'Authorization': f'Bearer {token}'}

# Reset workflow
req = urllib.request.Request('http://localhost:8000/api/workflows/WF-2026-0901/reset', data=b'', headers=headers)
urllib.request.urlopen(req, timeout=5)

print("Starting Flagship Multi-Agent Execution...")
for i in range(7):
    step_req = urllib.request.Request('http://localhost:8000/api/workflows/WF-2026-0901/next-step', data=b'', headers=headers)
    res = urllib.request.urlopen(step_req, timeout=5)
    data = json.loads(res.read().decode('utf-8'))
    step = data.get('executed_step', {})
    print(f"  Step {i+1}: [{step.get('status')}] {step.get('title')} -> Agent: {step.get('agent_name')}, Tool: {step.get('tool_used')}")

wf_req = urllib.request.Request('http://localhost:8000/api/workflows/WF-2026-0901', headers=headers)
with urllib.request.urlopen(wf_req, timeout=5) as resp:
    final_wf = json.loads(resp.read().decode('utf-8'))
print(f"\nFinal Workflow Status: {final_wf.get('status')}")
print(f"Context Refund ID: {final_wf.get('context', {}).get('refund_id')}")
print(f"Customer Response snippet: {final_wf.get('context', {}).get('final_response')[:120]}...")
