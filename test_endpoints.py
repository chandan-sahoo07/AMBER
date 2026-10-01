import pytest
from fastapi.testclient import TestClient
from backend.app.main import app, classify_intent, CASES_DB, APPROVALS_DB, orchestrator_engine

client = TestClient(app)

# Helper to get authenticated headers
def get_auth_headers(email: str, password: str = "password123"):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    token = res.json()["token"]
    return {"Authorization": f"Bearer {token}"}

# =====================================================
# S1 & S2: AUTHENTICATION, ESCALATION & BACKDOOR TESTS
# =====================================================

def test_login_success_and_user_profile():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["user"]["role"] == "ADMIN"
    assert data["user"]["email"] == "sarah.connor@support-ai.enterprise"

def test_login_role_escalation_attempt_ignored():
    # Attempting to supply role=ADMIN for a SUPPORT_AGENT account
    res = client.post("/api/auth/login", json={
        "email": "elena.rostova@support-ai.enterprise",
        "password": "password123",
        "role": "ADMIN"
    })
    assert res.status_code == 200
    user = res.json()["user"]
    # Real role in database is SUPPORT_AGENT, not client-supplied ADMIN
    assert user["role"] == "SUPPORT_AGENT"
    assert "all" not in user["permissions"]

def test_login_unknown_email_rejected_with_401():
    res = client.post("/api/auth/login", json={
        "email": "attacker@evil.corp",
        "password": "password123",
        "role": "ADMIN"
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."

def test_login_wrong_password_rejected_with_401():
    res = client.post("/api/auth/login", json={
        "email": "sarah.connor@support-ai.enterprise",
        "password": "wrongpassword123"
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."

def test_backdoor_passwords_removed():
    backdoors = ["admin123", "demo2026", "••••••••••••"]
    for pw in backdoors:
        res = client.post("/api/auth/login", json={
            "email": "sarah.connor@support-ai.enterprise",
            "password": pw
        })
        assert res.status_code == 401, f"Backdoor password '{pw}' was accepted!"

# =====================================================
# S3 & S4: TOKEN & SECURITY CHECKS
# =====================================================

def test_unauthenticated_access_rejected_with_401():
    protected_urls = [
        "/api/workflows",
        "/api/agents",
        "/api/approvals",
        "/api/audit-logs",
        "/api/analytics",
        "/api/tools"
    ]
    for url in protected_urls:
        res = client.get(url)
        assert res.status_code == 401, f"Endpoint {url} allowed unauthenticated access"

def test_token_via_query_param_rejected():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    token = headers["Authorization"].split(" ")[1]
    # Query param token must not be accepted
    res = client.get(f"/api/workflows?token={token}")
    assert res.status_code == 401

def test_sso_gated_by_environment():
    res = client.post("/api/auth/sso", json={"provider": "okta"})
    # Default is ENABLE_DEMO_SSO=false -> must return 403 Forbidden
    assert res.status_code in [403, 401]

# =====================================================
# S5: RBAC AUTHORIZATION CHECKS PER ROLE (403 FORBIDDEN)
# =====================================================

def test_viewer_role_forbidden_operations():
    viewer_headers = get_auth_headers("rachel.green@support-ai.enterprise")
    
    # 1. VIEWER cannot approve/reject
    res_appr = client.post("/api/approvals/APP-902/approve", headers=viewer_headers)
    assert res_appr.status_code == 403

    # 2. VIEWER cannot update agent prompts or tools
    res_agent = client.post("/api/agents/AG001_ORCHESTRATOR/update", json={"system_prompt": "new prompt"}, headers=viewer_headers)
    assert res_agent.status_code == 403

    # 3. VIEWER cannot read audit logs
    res_audit = client.get("/api/audit-logs", headers=viewer_headers)
    assert res_audit.status_code == 403

    # 4. VIEWER cannot create workflows
    res_wf = client.post("/api/workflows/create", json={"case_id": "CASE-8821", "customer_id": "CUST_101", "prompt": "test"}, headers=viewer_headers)
    assert res_wf.status_code == 403

    # 5. VIEWER cannot view or manage tools
    res_tools = client.get("/api/tools", headers=viewer_headers)
    assert res_tools.status_code == 403

def test_support_agent_role_forbidden_for_approvals():
    agent_headers = get_auth_headers("elena.rostova@support-ai.enterprise")
    res = client.post("/api/approvals/APP-902/approve", headers=agent_headers)
    assert res.status_code == 403

# =====================================================
# L2 & S6: APPROVAL STATE MACHINE & AUDIT ACTOR
# =====================================================

def test_approval_state_transitions_and_redecision_409():
    admin_headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    # Ensure APP-902 is in PENDING state for testing
    appr_target = next((a for a in APPROVALS_DB if a["id"] == "APP-902"), None)
    if appr_target:
        appr_target["status"] = "PENDING"

    # First decision: APPROVED
    res_approve = client.post("/api/approvals/APP-902/approve", headers=admin_headers)
    assert res_approve.status_code == 200
    assert res_approve.json()["status"] == "APPROVED"
    assert "Sarah Connor" in res_approve.json()["approved_by"]

    # Second decision attempt on already decided approval -> 409 Conflict
    res_reapprove = client.post("/api/approvals/APP-902/approve", headers=admin_headers)
    assert res_reapprove.status_code == 409

    # Reject attempt on already decided approval -> 409 Conflict
    res_rereject = client.post("/api/approvals/APP-902/reject", json={"reason": "changed mind"}, headers=admin_headers)
    assert res_rereject.status_code == 409

# =====================================================
# L1: WORKFLOW TEMPLATES & DYNAMIC CUSTOMER RESOLUTION
# =====================================================

def test_workflow_template_cust102_refund_runs_all_steps():
    admin_headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    # Create a refund workflow for CUST_102 (Priya Patel, ORD-45890)
    create_res = client.post("/api/workflows/create", json={
        "case_id": "CASE-8822",
        "customer_id": "CUST_102",
        "prompt": "Payment deducted for order, please refund.",
        "template": "refund"
    }, headers=admin_headers)
    assert create_res.status_code == 200
    wf_data = create_res.json()
    wf_id = wf_data["workflow_id"]
    assert wf_data["customer_id"] == "CUST_102"
    assert wf_data["template"] == "refund"

    # Execute all steps sequentially
    for _ in range(7):
        step_res = client.post(f"/api/workflows/{wf_id}/next-step", headers=admin_headers)
        assert step_res.status_code == 200

    # Retrieve final workflow instance
    final_res = client.get(f"/api/workflows/{wf_id}", headers=admin_headers)
    assert final_res.status_code == 200
    final_wf = final_res.json()
    assert final_wf["status"] == "COMPLETED"
    
    ctx = final_wf.get("context", {})
    assert "Priya Patel" in ctx.get("customer_name", "")
    assert ctx.get("refund_id") is not None
    assert "final_response" in ctx
    assert "Priya Patel" in ctx["final_response"]

# =====================================================
# L3: INTENT CLASSIFIER TEST CASES
# =====================================================

def test_intent_classifier_l3_benchmarks():
    test_cases = [
        ("won't pair with my phone", "HARDWARE_DIAGNOSTIC_FAILURE"),
        ("TV failed to connect via bluetooth", "HARDWARE_DIAGNOSTIC_FAILURE"),
        ("repair my laptop", "GENERAL_SUPPORT_QUERY"),
        ("cancel my order", "ORDER_CANCELLATION_REQUEST"),
        ("₹2000 deducted, order failed", "PAYMENT_CAPTURE_WITHOUT_ORDER")
    ]
    for text, expected_intent in test_cases:
        result = classify_intent(text)
        assert result["intent"] == expected_intent, f"Text '{text}' classified as '{result['intent']}', expected '{expected_intent}'"
        if expected_intent == "GENERAL_SUPPORT_QUERY":
            assert result["needs_human_review"] is True

# =====================================================
# L5: MESSAGE VALIDATION & REOPENING RESOLVED CASES
# =====================================================

def test_message_validation():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    # 1. Empty text after strip -> 422
    res_empty = client.post("/api/cases/CASE-8821/messages", json={"sender": "customer", "text": "   "}, headers=headers)
    assert res_empty.status_code == 422

    # 2. Text > 4000 characters -> 422
    res_long = client.post("/api/cases/CASE-8821/messages", json={"sender": "customer", "text": "a" * 4001}, headers=headers)
    assert res_long.status_code == 422

    # 3. Invalid sender -> 422
    res_sender = client.post("/api/cases/CASE-8821/messages", json={"sender": "unauthorized_bot", "text": "Hello"}, headers=headers)
    assert res_sender.status_code == 422

def test_customer_message_reopens_resolved_case():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    # First resolve the case
    client.post("/api/cases/CASE-8819/resolve", headers=headers)
    case_obj = next((c for c in CASES_DB if c["case_id"] == "CASE-8819"), None)
    assert case_obj["status"] == "RESOLVED"

    # Send message as customer
    res = client.post("/api/cases/CASE-8819/messages", json={
        "sender": "customer",
        "text": "The issue is still occurring, please reopen this."
    }, headers=headers)
    assert res.status_code == 200
    assert case_obj["status"] == "OPEN"

# =====================================================
# L8: ERROR HANDLING & 404 ON UNKNOWN IDS
# =====================================================

def test_unknown_id_endpoints_raise_404():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    endpoints = [
        "/api/workflows/WF-NONEXISTENT",
        "/api/workflows/WF-NONEXISTENT/next-step",
        "/api/workflows/WF-NONEXISTENT/reset",
        "/api/workflows/WF-NONEXISTENT/run-all",
        "/api/cases/CASE-NONEXISTENT",
        "/api/customers/CUST-NONEXISTENT"
    ]
    for ep in endpoints:
        if "next-step" in ep or "reset" in ep or "run-all" in ep:
            res = client.post(ep, headers=headers)
        else:
            res = client.get(ep, headers=headers)
        assert res.status_code == 404, f"Endpoint {ep} did not return 404: {res.status_code}"

# =====================================================
# L7: AUDIT HASH CHAIN VERIFICATION
# =====================================================

def test_audit_hash_chain_integrity():
    headers = get_auth_headers("david.chen@support-ai.enterprise") # Auditor
    res = client.get("/api/audit-logs/verify", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["verified"] is True
    assert data["total_records"] > 0

# =====================================================
# CALLING SYSTEM: AI & EXECUTIVE VOIP LINK TESTS
# =====================================================

def test_list_calls_and_initial_record():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    res = client.get("/api/calls", headers=headers)
    assert res.status_code == 200
    calls = res.json()
    assert isinstance(calls, list)
    assert len(calls) >= 1
    assert any(c["call_id"] == "CALL-7701" for c in calls)

def test_initiate_ai_voice_call_and_dialogue():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")
    
    # 1. Initiate call to AI
    res = client.post("/api/calls/initiate", json={
        "customer_id": "CUST_101",
        "case_id": "CASE-8821",
        "target_type": "AI",
        "topic": "UPI Payment Deducted Without Order"
    }, headers=headers)
    assert res.status_code == 200
    call_data = res.json()
    call_id = call_data["call_id"]
    assert call_data["target_type"] == "AI"
    assert call_data["status"] == "IN_PROGRESS"
    assert "AMBER" in call_data["agent_name"]
    assert len(call_data["transcript"]) >= 1
    assert call_data["transcript"][0]["speaker"] == "ai"

    # 2. Customer speaks to AI regarding refund
    speak_res = client.post(f"/api/calls/{call_id}/speak", json={
        "speaker": "customer",
        "text": "₹2,000 was deducted from my UPI account but order failed! Please issue refund."
    }, headers=headers)
    assert speak_res.status_code == 200
    updated_call = speak_res.json()
    assert len(updated_call["transcript"]) >= 3
    # Check that AI responded
    ai_turn = updated_call["transcript"][-1]
    assert ai_turn["speaker"] == "ai"
    assert len(ai_turn["text"]) > 20
    assert any(k in ai_turn["text"].lower() for k in ["transaction", "refund", "support", "verification", "order", "policy", "amber"])

    # 3. Hold and resume
    hold_res = client.post(f"/api/calls/{call_id}/hold", headers=headers)
    assert hold_res.status_code == 200
    assert hold_res.json()["is_on_hold"] is True

    # 4. End call
    end_res = client.post(f"/api/calls/{call_id}/end", json={
        "summary": "AI confirmed ₹2,000 refund transaction and informed customer."
    }, headers=headers)
    assert end_res.status_code == 200
    ended_call = end_res.json()
    assert ended_call["status"] == "ENDED"
    assert ended_call["duration_seconds"] > 0
    assert "refund" in ended_call["summary"].lower()

def test_initiate_executive_call_answer_and_transfer():
    headers = get_auth_headers("sarah.connor@support-ai.enterprise")

    # 1. Initiate call to Executive
    res = client.post("/api/calls/initiate", json={
        "customer_id": "CUST_102",
        "case_id": "CASE-8822",
        "target_type": "EXECUTIVE",
        "topic": "Priority Logistics Escalation"
    }, headers=headers)
    assert res.status_code == 200
    call_data = res.json()
    call_id = call_data["call_id"]
    assert call_data["target_type"] == "EXECUTIVE"
    assert call_data["status"] == "RINGING"
    assert call_data["agent_name"] == "Ananya Iyer"

    # 2. Executive answers call
    ans_res = client.post(f"/api/calls/{call_id}/answer", headers=headers)
    assert ans_res.status_code == 200
    answered_call = ans_res.json()
    assert answered_call["status"] == "IN_PROGRESS"
    assert any(t["speaker"] == "executive" for t in answered_call["transcript"])

    # 3. Customer speaks to Executive
    speak_res = client.post(f"/api/calls/{call_id}/speak", json={
        "speaker": "customer",
        "text": "Hello, I need urgent priority shipping for tomorrow morning."
    }, headers=headers)
    assert speak_res.status_code == 200

    # 4. Executive speaks back
    exec_speak = client.post(f"/api/calls/{call_id}/speak", json={
        "speaker": "executive",
        "text": "I can certainly arrange priority air freight dispatch for you right away."
    }, headers=headers)
    assert exec_speak.status_code == 200

    # 5. Transfer to AI
    trans_res = client.post(f"/api/calls/{call_id}/transfer", json={
        "target_type": "AI",
        "reason": "Transfer to AI for automated telemetry check"
    }, headers=headers)
    assert trans_res.status_code == 200
    trans_call = trans_res.json()
    assert trans_call["target_type"] == "AI"

    # 6. End call
    end_res = client.post(f"/api/calls/{call_id}/end", headers=headers)
    assert end_res.status_code == 200
    assert end_res.json()["status"] == "ENDED"
