# Lambda CSV processing service

Service Python 3.14 xử lý CSV từ S3. Tương tự `uretailV2`, API và Lambda handler chạy
trên máy bằng Serverless Offline, còn S3 sử dụng AWS thật qua IAM profile hoặc SSO. Chạy local không
cần deploy Lambda hoặc Docker.

## Chuẩn bị

- Node.js 22.11.0, Yarn 4.18.1.
- Python 3.14 và Poetry 2.5.1 (`python -m pip install poetry==2.5.1`). Lệnh `python` có thể là
  bản Python khác đã cài Poetry; Poetry sẽ dùng môi trường Python 3.14 của dự án.
- AWS CLI v2, Git Bash để chạy `yarn aws:login` trên Windows.
- AWS profile có quyền đọc/ghi object dưới `input/` trong bucket `s3-sls-demo`.

Chạy trong thư mục dự án:

```powershell
# Chỉ copy khi chưa có .env; giữ cấu hình đã có nếu file tồn tại.
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -m poetry env use 3.14
yarn install:all
```

Nếu Python 3.14 được cài ở vị trí riêng, truyền đường dẫn `python.exe` cho
`python -m poetry env use` thay vì `3.14`.

## Cấu hình AWS và chạy local

Điền `.env`:

```dotenv
AWS_PROFILE=lambda-local
AWS_SSO_PROFILE=
AWS_REGION=ap-southeast-2
CSV_BUCKET_NAME=s3-sls-demo
CSV_BUCKET_EXISTING=true
CSV_INPUT_PREFIX=input/
```

Với IAM user, cấu hình bằng `aws configure --profile lambda-local` rồi chạy
`yarn run:local`; không cần `yarn aws:login`. Bucket này nằm ở `ap-southeast-2`,
tài khoản `426187995294`. Nếu dùng SSO, đặt `AWS_SSO_PROFILE` tương ứng. Region của SSO có thể
khác region của S3. Nếu chưa có profile, chạy `aws configure sso --profile <tên-profile>`
với SSO Start URL, SSO region, account và role được cấp.

```powershell
yarn aws:login
yarn run:local
```

API mặc định: `http://127.0.0.1:3000/local`. Local không yêu cầu `x-api-key`.
`GET /health` trả HTTP 200 với `{"status":"ok"}` để kiểm tra API Gateway và Lambda
phản hồi được. Đây là kiểm tra liveness; quyền truy cập S3 được kiểm tra qua luồng CSV.
Đổi cổng khi `uretailV2` đang chiếm cổng 3000:

```powershell
yarn run:local --httpPort 3010 --lambdaPort 3012
```

Các lệnh local tự nạp `.env.aws` và `.env`. Biến đã đặt trong terminal được ưu tiên;
credentials xuất trong `.env.aws` được ưu tiên hơn `.env`. Khi không có access key
đã xuất, loader dùng `AWS_PROFILE` hoặc `AWS_SSO_PROFILE` qua credential chain của AWS.
Có thể dùng `aws sso login --profile <tên-profile>` thay cho `yarn aws:login` để
không xuất credentials ra file; khi đó cần bỏ `.env.aws` cũ nếu đã hết hạn.
Đăng nhập lại khi phiên SSO/credentials hết hạn. Không commit các file credentials.

## Thử upload và xử lý CSV

Nếu dùng Postman, import [collection local](examples/lambda-local.postman_collection.json)
rồi gửi lần lượt ba request theo số thứ tự. Collection tự lưu upload URL và S3 key,
đã chứa CSV mẫu hai dòng. Bước 2 ghi CSV lên bucket AWS thật. Nếu đổi cổng API,
cập nhật biến collection `base_url`. Giữ terminal `yarn run:local` mở trong lúc thử.

Mở PowerShell thứ hai trong thư mục dự án, giữ API đang chạy:

```powershell
$baseUrl = 'http://127.0.0.1:3000/local'
$upload = Invoke-RestMethod -Method Post -Uri "$baseUrl/csv/upload-url" `
  -ContentType 'application/json' -Body '{"file_name":"orders.csv"}'

Invoke-WebRequest -UseBasicParsing -Method Put -Uri $upload.upload_url `
  -ContentType 'text/csv' -InFile .\examples\orders.csv | Out-Null

$body = @{ key = $upload.key } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$baseUrl/csv/import" `
  -ContentType 'application/json' -Body $body
```

Kết quả mẫu: `total_rows=2`, `valid_rows=2`, `invalid_rows=0`.
Upload ghi file vào bucket AWS thật. Serverless Offline không nhận sự kiện S3;
`/csv/import` xử lý đồng bộ object vừa upload. Các notification đã cấu hình trên
bucket AWS vẫn hoạt động độc lập.

Có thể gọi trực tiếp handler với key đã upload, không cần HTTP server:

```powershell
yarn invoke:api --key 'input/<uuid>/orders.csv'
yarn invoke:s3 --key 'input/<uuid>/orders.csv'
```

`invoke:s3` tạo sự kiện ObjectCreated giả lập trên máy rồi đọc object từ S3 thật.
Schema hiện tại là mẫu gồm `id`, `name`, `quantity`; `quantity` phải là số nguyên
không âm. Thay `src/csv_processing/models.py` khi có đặc tả CSV nghiệp vụ.

## Kiểm tra

```powershell
yarn check-all
```

Gồm Ruff, Black, mypy và pytest. Unit test sử dụng Moto với credentials giả lập,
không cần AWS SSO và không đọc/ghi bucket thật.

## Lỗi thường gặp

- `CERTIFICATE_VERIFY_FAILED` trên mạng công ty: lấy CA bundle PEM từ IT và đặt
  `AWS_CA_BUNDLE=<đường-dẫn-tuyệt-đối-đến-file-PEM>` trong `.env` cho ứng dụng local.
  AWS CLI chạy trực tiếp không tự đọc `.env`; đặt `$env:AWS_CA_BUNDLE` trong PowerShell
  hoặc cấu hình `ca_bundle` cho profile. Không tắt xác minh SSL.
- `CSV_BUCKET_NAME` thiếu/rỗng: cập nhật `.env`; startup sẽ báo lỗi trước khi mở API.
- Profile không tồn tại: chạy `aws configure sso --profile <tên-profile>`.
- Token hết hạn: chạy lại `yarn aws:login` rồi khởi động lại API.
- `AccessDenied`: kiểm tra profile/account và quyền S3; xác nhận region của bucket.
- Deploy báo `Unexpected close tag`: kiểm tra phản hồi mạng. Nếu gateway công ty trả
  HTTP 403 / `Block upload source code`, cần IT phê duyệt cho phép upload mã nguồn
  tới bucket deployment của AWS. Thêm quyền IAM hoặc thay CA không giải quyết rule này.
- `OBJECT_NOT_FOUND`: dùng đúng `key` trả về từ upload URL sau khi PUT thành công.
- Python không đúng phiên bản: chọn Python 3.14 bằng `python -m poetry env use`.

## Deploy AWS (tách biệt với local)

Các lệnh bên dưới chạy trên Windows PowerShell, macOS và Linux, tự nạp `.env` /
`.env.aws`, IAM profile và CA bundle. Bật Docker Desktop với Linux engine trước khi đóng gói.
Trên máy macOS hiện tại, chạy `source .tools/activate.sh` trong terminal dự án để
dùng Python và Poetry đã cài riêng. Runner build gói Linux x86_64 cả trên máy Apple Silicon.
Poetry cần plugin `poetry-plugin-export==1.10.1` (đã khai báo trong `pyproject.toml`).
Nếu Poetry không cài được plugin vì môi trường toàn máy có dependency hỏng,
cài riêng bằng `python -m pip install poetry-plugin-export==1.10.1`.

```powershell
aws sts get-caller-identity --profile lambda-local
yarn serverless login # Serverless v4 yêu cầu đăng nhập riêng, ngoài AWS profile
yarn check-all
yarn package:dev   # Chỉ tạo gói và CloudFormation template, chưa triển khai
yarn deploy:dev    # Đóng gói rồi triển khai stage dev theo AWS_REGION trong .env
yarn info:dev      # Xem endpoint sau triển khai
```

Serverless Framework được cố định ở `4.43.0`, hỗ trợ runtime `python3.14` và có
built-in Python requirements; không cài thêm `serverless-python-requirements`.
Serverless Offline `14.8.3` còn thiếu Python 3.14 trong danh sách runtime. Bản vá
`.yarn/patches/serverless-offline-*.patch` bổ sung runtime/architecture và được Yarn
tự áp dụng khi `yarn install`; cần giữ bản vá cùng `yarn.lock` trong Git.

Đăng nhập Serverless một lần bằng `yarn serverless login`; CI dùng biến môi trường
`SERVERLESS_ACCESS_KEY` hoặc `SERVERLESS_LICENSE_KEY` phù hợp với tài khoản.

Tài khoản đích của dự án là `426187995294`, region `ap-southeast-2`. Với bucket
`s3-sls-demo` có sẵn, giữ `CSV_BUCKET_EXISTING=true`. Deploy cần quyền CloudFormation,
Lambda, API Gateway, IAM, S3 và CloudWatch Logs ngoài quyền đọc/ghi CSV ở local.
`yarn deploy:dev` đóng gói trước và chỉ triển khai khi bước đóng gói thành công.

Gói build luôn dùng Docker image `public.ecr.aws/sam/build-python3.14:latest-x86_64`
để cài dependency Python 3.14 / Linux x86_64, kể cả trên host Linux ARM64.
CA công ty chỉ được mount read-only vào
Docker để tải dependency; không đưa CA, `.env`, credentials hoặc file test vào Lambda.
Helper cấu hình notification của bucket có sẵn dùng Node.js 22.
Plugin `scripts/normalize_package.cjs` chuẩn hóa đường dẫn ZIP thành dấu `/`
cho cả gói dùng chung và gói riêng trước khi Serverless tính checksum, để dependency
đóng gói trên Windows import được trên Linux.

Lambda `health` được đóng gói riêng thành `.serverless/health.zip`, chỉ chứa
`src/handlers/health.py` và hai file `__init__.py`, không có package ngoài.
Hai Lambda CSV tiếp tục dùng chung gói `csv-lambda.zip` cùng dependency từ Poetry.
Chỉ `health` bật `package.individually`. Vì bộ đóng gói tích hợp của v4 có thể chèn
dependency chung vào cả gói riêng, bước `normalize_package.cjs` áp dụng danh sách
file nguồn trong `health.package.patterns` sau khi chèn dependency và trước khi tính
checksum/upload. Bước này loại mọi package ngoài khỏi `health.zip` và báo lỗi nếu
thiếu file nguồn bắt buộc; gói CSV vẫn giữ đầy đủ dependency.

Sau khi deploy, gọi `GET <base_url>/health` không cần API key, ví dụ:

```bash
curl https://rmp58jkj6h.execute-api.ap-southeast-2.amazonaws.com/dev/health
# {"status": "ok"}
```

Sau khi deploy, API CSV yêu cầu header `x-api-key`. Lấy key trong AWS Console → API Gateway
→ API keys, rồi đặt vào request tạo upload URL và import trong Postman; request PUT
S3 dùng presigned URL. Upload `input/*.csv` trên AWS sẽ tự kích hoạt `csvS3Import`.
Kết quả xử lý nằm trong CloudWatch Logs của `csv-lambda-dev-csvS3Import`.

Import [collection AWS dev](examples/lambda-aws-dev.postman_collection.json) để thử
ba bước upload/import. Đặt `base_url` bằng endpoint từ `yarn info:dev`, `api_key`
bằng key `dev-csv-lambda-api-key`, rồi gửi lần lượt request 1, 2, 3.
Nếu có file `.tools/deploy/aws-dev.postman_environment.json` do bước kiểm tra triển
khai tạo ra, import file này và chọn environment tương ứng để nạp sẵn endpoint/key.
File environment chứa API key nên chỉ giữ trên máy, không commit hoặc chia sẻ.
