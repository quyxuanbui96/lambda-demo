# Acquisition/transfer backend

Bản triển khai này bám vào WBS và tóm tắt 8 API đã cung cấp. Đã có backend chạy được cho hai workflow, PostgreSQL persistence, SQS worker/recovery, S3 versioning và kiểm thử. Đây là **draft API/business contract**, chưa thay thế Detail Design đầy đủ.

Các quyết định đã được xác nhận trong trao đổi: PostgreSQL; SQS + scheduler; tạm bypass OCR authentication để tích hợp sau. Bypass dùng một identity/organization allowlist cấu hình sẵn, không lấy user/organization từ headers tùy ý. Chỉ cho phép bypass trong `local`/`dev`; `prod`/`uat` cần context từ authorizer đã xác minh. Không có login, JWT riêng, cursor hay JSON-page preprocessing.

## Chạy demo đủ 8 API

```bash
source .tools/activate.sh
python -m poetry install
yarn workflow:local
# http://127.0.0.1:3000
# Dừng bằng Ctrl+C
```

Đổi cổng: `yarn workflow:local --port 3010 --s3-port 5002`.

Demo khởi tạo **SQLite, S3 và SQS giả lập** dùng fake credentials và endpoint localhost. Không đọc/ghi AWS thật. DB và object demo được tạo mới mỗi lần chạy và xóa khi dừng; đây không phải môi trường RDS production. Worker đọc queue và xử lý nền trong tiến trình local. Route `/health` vẫn là stdlib handler. `yarn run:local` tiếp tục chạy CSV demo cũ.

Dữ liệu mẫu: `userId=demo-user`, `organizationId=org-demo`, `participantCode="00001"`, `insuranceYear=2026`, `businessType=DEMO`. Source mẫu có thêm organization/participant khác để chứng minh filtering. Layout output là `demo-v1`, outputType `DEMO_OUTPUT`.

1. `GET /session/context` xem user và organization.
2. `POST /acquisitions`:

```json
{"operationKey":"<new UUID>","organizationId":"org-demo","participantCode":"00001","insuranceYear":2026,"businessType":"DEMO"}
```

3. Poll `GET /acquisitions/{acquisitionId}` đến `READY`.
4. `POST /acquisitions/{acquisitionId}/download-url` với `{"fileId":"<generated file UUID>"}`, tải bằng `GET downloadUrl`.
5. Tính SHA-256 lowercase hex của **bytes file sẽ upload**. `POST /transfers`:

```json
{"operationKey":"<new UUID>","acquisitionId":"<UUID>","outputType":"DEMO_OUTPUT","layoutVersion":"demo-v1","contentLength":123,"contentDigest":"<64 lowercase hex characters>"}
```

6. `POST /transfers/{transferId}/upload-url` không body hoặc `{}`. `PUT uploadUrl` với **toàn bộ requiredHeaders** và bytes file. File đi trực tiếp tới S3, không qua API Lambda.
7. `POST /transfers/{transferId}/complete` không body hoặc `{}`.
8. `GET /transfers/{transferId}` xem `TRANSFERRED` và `completedAt`.

`operationKey` scoped theo user + operation type. Cùng key/payload trả job cũ; payload khác trả `409 E3002`. `participantCode` bắt buộc là string, giữ số 0 đầu. API02 mới trả 202, replay 200; API05 mới trả 201, replay 200. Đây là các lựa chọn draft cần đối chiếu Detail Design chính thức.

## State/transaction/recovery

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> RUNNING: worker claims lease
    RUNNING --> READY: immutable generated file + DB commit
    RUNNING --> QUEUED: transient dependency error / expired lease
    RUNNING --> FAILED: permanent error or retry limit
```

Transfer: `PREPARED → TRANSFERRED/FAILED`. Acquisition `FAILED` vẫn trả HTTP 200 khi tra cứu. Chỉ `READY` có `sourceVersions/files`; chỉ `FAILED` có `error`. Transfer chưa xong có `completedAt=null`.

Job, operation reservation, dispatch record và audit được ghi cùng transaction. Unique constraint và savepoint xử lý concurrent operation keys; optimistic version + row locks bảo vệ update. Worker chỉ dispatch sau commit. Dispatch lỗi giữ `PENDING`; scheduler chạy mỗi phút gửi lại job hoặc thu hồi lease hết hạn. Worker tối đa 3 lần, lease 420 giây, timeout 300 giây; duplicate event không tạo file trùng. Nguồn/layout được snapshot trước xử lý; retry không tự đổi sang source version mới.

Generated file được tạo trên disk tạm bằng streaming CSV, có giới hạn record/file. Chỉ giữ row khớp đủ organization/participant/year/businessType và column được đăng ký. Zero matching rows tạo file chỉ có header. Source thiếu/malformed/layout sai trở thành job FAILED. Mapping hiện là configurable projection/filter; các phép biến đổi/tổng hợp nghiệp vụ OCR/TISI chưa được cung cấp.

Complete pin temporary VersionId trong DB trước validation, stream để kiểm tra SHA-256 và layout, rồi conditional CopyObject (`If-None-Match: *`) sang final key do server sinh. Final metadata gắn transfer/digest/layout/source-version. Retry sau copy-success/DB-failure tìm lại đúng final version; không copy lần hai. `completedAt` lấy từ thời gian object final, giữ cố định khi retry. Cleanup có thể reconcile final copy đã tồn tại trước khi xóa temp. Bucket bắt buộc versioning; không dùng `null` version.

URL PUT ký `Content-Type`, SHA-256 checksum, transfer/layout metadata và `If-None-Match: *`. Retry PUT sau một upload thực sự đã thành công có thể trả 412; client nên gọi complete để xác nhận thay vì ghi đè object. TTL mặc định 300 giây; retention temp/reservation 24 giờ; polling gợi ý 3 giây (GET pending trả Retry-After). Những giá trị này là cấu hình draft chờ chốt D08/D09, không phải thông số đã được khách hàng phê duyệt. Cleanup xóa cả version/delete marker của temp; final/generated không có policy tự xóa cho đến khi retention nghiệp vụ được chốt.

## PostgreSQL, source catalog và migration

Schema: `migrations/001_workflow.up.sql`, rollback: `001_workflow.down.sql`. Bảy bảng: acquisition_job, transfer_job, managed_file, source_file_version, operation_request, dispatch_state, audit_log. Các cột lookup/status/version/FK/unique/index được tách riêng; payload JSON giữ snapshot model. Runtime Lambda **không tự tạo schema**.

```bash
yarn workflow:migrate --export-sql   # cập nhật SQL từ schema code
yarn workflow:migrate               # apply schema, cần DB config; idempotent create tables
```

Rollback xóa các bảng/dữ liệu workflow: chỉ chạy SQL down khi DB team đã backup và xác nhận. Migration hiện là initial schema; thay đổi schema tiếp theo cần revision SQL mới, không dựa vào create_all để alter bảng.

Source discovery đọc `source_file_version`: chọn newest captured version theo scope và từng `(sourceSystem,bucket,key)`. OCR/TISI team phải đăng ký versioned source catalog qua adapter/importer backend (không có API public cho raw S3 key). Có CLI `workflow_register_source.py --manifest <json-file>` để import manifest bằng DB credential đã cấu hình. Không có code gọi source system thật khi chưa có interface/source schema.

Workflow ZIP giữ `.dist-info` để psycopg nhận đúng version và SQLAlchemy khởi tạo driver;
slim chỉ xóa pycache/pyc, health vẫn loại mọi dependency.
PostgreSQL driver là `psycopg` binary cho Linux/Python 3.14. Engine pool reuse, pre-ping, connection/query/lock timeout; Secrets Manager cache TTL 300s, credential refresh khi mở connection mới. TLS `verify-full` với public [AWS RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem) đóng gói tại `certs/global-bundle.pem`.

## DEV thật và infrastructure

`serverless.workflow.yml` là **service riêng** `ocr-file-workflow`, tránh thay đổi stack CSV demo đang chạy. Nó khai báo versioned/encrypted/private S3 bucket, SQS + DLQ, worker event source, scheduler, execution roles tách API/worker/maintenance và alarm. Build/deploy runner hỗ trợ Windows/macOS/Linux. Dependency DB nằm nhóm Poetry `workflow`; ZIP CSV cũ chỉ dùng main dependencies. `health.zip` luôn được lọc về đúng ba file nguồn, không có package ngoài.

Cần bổ sung `.env` trước khi deploy:

```dotenv
AWS_REGION=ap-southeast-2
WF_DEPLOYMENT_BUCKET=<existing artifact bucket name>
WF_BUCKET=<new workflow bucket name>
WF_DATABASE_SECRET_ARN=<Secrets Manager ARN>
WF_DATABASE_SSL_ROOT_CERT=certs/global-bundle.pem
WF_CONTRACTS_PATH=examples/workflow/contracts.demo.json
WF_AUTH_MODE=bypass
WF_DEV_IDENTITY={"userId":"demo-user","allowedOrganizations":[{"organizationId":"org-demo","displayName":"Demo organization"}]}
WF_VPC_SUBNET_IDS=subnet-...,subnet-...
WF_VPC_SECURITY_GROUP_IDS=sg-...
```

Deployment bucket được cấu hình explicit để không cần Serverless tạo SSM deployment-bucket parameter.
DB secret có host, port (string/integer), dbname, username, password. Runtime dùng secret ARN, không đưa credential vào Lambda env/template. DB team cấp quyền app SELECT/INSERT/UPDATE trên bảy bảng và USAGE cho sequence `operation_request`; dùng migration role riêng cho DDL. Private subnet cần route/VPC endpoints cho S3, SQS, Secrets Manager và Logs; SG cho phép kết nối RDS 5432. Nếu secret dùng customer-managed KMS key, Infra phải cấp `kms:Decrypt` đúng key cho execution roles.

```bash
yarn check-all
yarn workflow:test
yarn workflow:package   # chỉ build, chưa deploy
yarn workflow:verify    # kiểm tra source, ABI, health và checksum của artifact
yarn workflow:deploy    # chỉ dùng khi RDS/VPC/migration/config đã sẵn sàng
yarn workflow:info
```

Trong runtime `authorizer` mode, adapter nhận JSON identity từ `requestContext.authorizer.identity`: userId và allowedOrganizations. Infra/OCR team cần gắn authorizer xác minh credential hiện có cho 8 route; `WF_AUTH_MODE=authorizer` một mình không tạo hoặc cấu hình OCR verifier. Không tự phát hành token. Credential thiếu/sai/mapping lỗi trả E2001; thiếu organization không cho phép truy cập resource.

API log action/requestId/userId/resourceId/httpStatus/error/duration. Audit log thêm organization, trạng thái nghiệp vụ; không ghi credential, URL ký, nội dung CSV hoặc participant PII. Audit transition logs trong transaction có thể precede commit; durable audit nằm ở DB. API metrics EMF, dispatch failures và stale recovery có namespace OCRFileWorkflow. DLQ có alarm, role phân prefix/queue/secret. Infra cần nối alarm actions/SNS, chỉnh threshold/retention và review network/KMS trước release.

## Kiểm thử

```bash
yarn check-all                # legacy + workflow unit/repository/service tests
yarn workflow:test            # coverage src.workflow >=80%
python -m poetry run pytest tests/integration -m integration
```

Integration HTTP thực sự chạy đủ 8 API với Moto S3/SQS endpoint trên localhost, PUT/GET presigned URL, asynchronous worker và disposable DB; tự tắt server khi xong. Để test repository bằng PostgreSQL riêng:

```bash
TEST_WORKFLOW_POSTGRES_URL='postgresql+psycopg://test-user:test-password@127.0.0.1:55432/test-db' \
  python -m poetry run pytest tests/workflow
```

Chỉ dùng dedicated test database/user có CREATE SCHEMA: mỗi test tạo schema UUID riêng và xóa đúng schema đó, không tạo/xóa bảng của schema ứng dụng. Coverage gate không thay cho test OCR authentication, RDS/VPC thật hoặc Excel/UAT.

## Các đầu vào còn cần chốt

| Đầu vào | Trạng thái |
|---|---|
| Full Detail Design/Basic Design D01–D12: format field, UUID/key scope, status/errors/nullability | Chưa được cung cấp; OpenAPI hiện là draft |
| OCR credential verifier / authorizer / permission source | Deferred theo yêu cầu; có bypass + adapter boundary |
| OCR/TISI discovery integration, layouts và business transformations | Chưa được cung cấp; demo contract/manifest có thể thay |
| RDS Secret ARN, schema ownership và VPC/SG | PostgreSQL đã chọn; endpoint/config chưa có |
| Production max size, TTL, retention, poll interval, zero-record policy | Cấu hình provisional, cần chốt |
| Real DEV deployment, OCR/RDS/Excel integration, alarms delivery, UAT/PROD | Chưa chạy; cần infrastructure và nghiệp vụ thật |

OpenAPI: `docs/openapi.workflow.json`. Các error code ngoài E2001/E3002 là catalog draft: E1001 validation, E1002 unsupported contract, E1004 route, E2002 authorization, E3001 not-found, E3003 state/concurrency, E3004 expired reservation, E4001 missing upload, E4002 size, E4003 layout/CSV, E4004 source, E4005 digest, E5000 internal, E5001 config, E5002 transient dependency, E5003 object reconciliation.

## Evidence kiểm tra bản hiện tại

Đã pass lint/Black/mypy; unit/service/repository tests; 8-API HTTP E2E với S3/SQS giả lập;
workflow tests chạy trên PostgreSQL 17 trong Docker và coverage trên 90%.
ZIP được kiểm tra source/architecture/checksum; chính driver Linux đóng trong ZIP đã
ghi/đọc transaction PostgreSQL thật. `health.zip` 712 bytes, đúng ba file stdlib.
CloudFormation lint không có error; có W3005 về DependsOn dư do Serverless sinh cùng GetAtt role.
CI đã viết tại `.github/workflows/check.yml`, chưa có run GitHub cho thay đổi local này.
Những kiểm tra trên không bao gồm OCR/TISI/RDS DEV thật hoặc Excel/UAT.
