# Local Lambda Server Implementation Plan

## Overview

Replace AWS Lambda calls with a local HTTP service running the same Python code, reducing costs while maintaining identical functionality.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     primemover-orchestrator                      │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ orchestrator/                                                ││
│  │   components/                                                ││
│  │     lambda_server.py  ← NEW: Component for lifecycle mgmt   ││
│  │   lambda_service/     ← NEW: The actual Flask service       ││
│  │     server.py         ← HTTP wrapper                        ││
│  │     decision/         ← Symlink to bot-decision-yt code     ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP POST localhost:5001/invoke
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      primemover_runner                           │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ LambdaCaller.java                                            ││
│  │   - Check lambda.mode config (local vs aws)                 ││
│  │   - If local: POST to localhost:5001/invoke                 ││
│  │   - If aws: Use AWS SDK (existing code)                     ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

## Implementation Tasks

### Phase 1: Lambda Service in Orchestrator

#### 1.1 Create Flask Service (`orchestrator/lambda_service/server.py`)
```python
from flask import Flask, request, jsonify
import json
import sys
sys.path.insert(0, 'decision')
from lambda_function import select_recommendation

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"})

@app.route('/invoke', methods=['POST'])
def invoke():
    try:
        event = request.json
        result = select_recommendation(json.dumps(event))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "xpath": ""}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001)
```

#### 1.2 Symlink bot-decision-yt code
```bash
cd orchestrator/lambda_service
ln -s /home/primemover/bot-decision-yt/bot-decision-EN decision
```

#### 1.3 Add dependencies to pyproject.toml
- flask
- (bot-decision-yt deps: lxml, pandas, openai, langdetect)

### Phase 2: Lambda Server Component (`orchestrator/components/lambda_server.py`)

#### 2.1 Create LambdaServerComponent class
- Extends BaseComponent
- Manages Flask server as subprocess
- start(): Launch `python -m orchestrator.lambda_service.server`
- stop(): Kill subprocess
- status(): Check if process running, health endpoint responds

#### 2.2 Configuration
```python
DEFAULT_PORT = 5001
DEFAULT_HOST = '127.0.0.1'
```

### Phase 3: Integrate into ComponentManager

#### 3.1 Update `manager.py`
- Add `_lambda_server: Optional[LambdaServerComponent]`
- Add to STARTUP_ORDER (before runner, since runner needs it)
- Add to start/stop/restart/status methods
- Update SystemStatus dataclass

#### 3.2 Update CLI (`cli.py`)
- Add `--no-lambda-local` flag to `up` command
- Add `--lambda-local` flag to show it's default
- Update status display

### Phase 4: Runner Changes

#### 4.1 Configuration (`application.properties`)
```properties
# Lambda execution mode: 'local' or 'aws'
lambda.mode=local
lambda.local.url=http://localhost:5001/invoke
lambda.local.timeout=120
```

#### 4.2 Modify `LambdaCaller.java`
```java
@Value("${lambda.mode:aws}")
private String lambdaMode;

@Value("${lambda.local.url:http://localhost:5001/invoke}")
private String localLambdaUrl;

public InvokeResponse invoke(String functionName, String param) throws LambdaException {
    if ("local".equals(lambdaMode)) {
        return invokeLocal(param);
    } else {
        return invokeAws(functionName, param);
    }
}

private InvokeResponse invokeLocal(String param) throws LambdaException {
    // Use RestTemplate to POST to local server
    // Convert response to InvokeResponse-like object
}
```

#### 4.3 Create `LocalLambdaResponse` class
- Wrapper to match AWS SDK InvokeResponse interface
- Or modify handler to accept JSON response directly

### Phase 5: Testing

#### 5.1 Unit tests
- Lambda service endpoint tests
- LambdaCaller local mode tests

#### 5.2 Integration tests
- E2E test with local Lambda
- Verify identical behavior to AWS Lambda

## File Changes Summary

| Repo | File | Action |
|------|------|--------|
| orchestrator | `orchestrator/lambda_service/__init__.py` | Create |
| orchestrator | `orchestrator/lambda_service/server.py` | Create |
| orchestrator | `orchestrator/lambda_service/decision` | Symlink |
| orchestrator | `orchestrator/components/lambda_server.py` | Create |
| orchestrator | `orchestrator/components/manager.py` | Modify |
| orchestrator | `orchestrator/cli.py` | Modify |
| orchestrator | `pyproject.toml` | Add deps |
| runner | `LambdaCaller.java` | Modify |
| runner | `application.properties` | Add config |

## Configuration

### Orchestrator (.env or defaults)
```
LAMBDA_SERVER_PORT=5001
LAMBDA_SERVER_HOST=127.0.0.1
```

### Runner (application.properties)
```properties
lambda.mode=local
lambda.local.url=http://localhost:5001/invoke
```

## Rollback

To use AWS Lambda instead:
1. Set `lambda.mode=aws` in runner config
2. Run `orchestrator up --no-lambda-local`

## Cost Savings

- AWS Lambda: ~$0.20/1M requests + $0.0000166667/GB-s compute
- Local: $0 (runs on existing infrastructure)
- For typical usage (1000s of calls/day): Savings of $10-50/month
