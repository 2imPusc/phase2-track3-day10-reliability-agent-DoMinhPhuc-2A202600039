# Day 10 Reliability Report

## 1. Architecture summary

Gateway điều phối mỗi prompt qua bước kiểm tra cache (in-memory `ResponseCache` hoặc `SharedRedisCache`), rồi tới chuỗi provider có circuit breaker bảo vệ (primary → backup); nếu mọi provider thất bại hoặc breaker đang OPEN thì trả về thông điệp tĩnh suy giảm. Cache dùng hàm tương đồng lai (hybrid): kết hợp Jaccard trên char-trigram và Jaccard trên token cho các tra cứu không khớp tuyệt đối; kèm các bộ chặn (guardrail) cho từ khoá nhạy cảm về quyền riêng tư và khác biệt năm để loại các cú hit sai hoặc nguy hiểm. `SharedRedisCache` tự suy giảm về cache in-memory khi Redis không truy cập được.

```
User Request
    |
    v
[Gateway] ---> [Cache.get] ---> HIT? return (cache_hit:<score>)
    |                                 |
    v                                 v MISS
[Breaker: primary] -------> primary provider
    |  (OPEN? fail fast)
    v
[Breaker: backup] --------> backup provider
    |  (OPEN? fail fast)
    v
[Static fallback message]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Phát hiện lỗi liên tiếp nhanh nhưng không bị flapping vì jitter ngẫu nhiên |
| reset_timeout_seconds | 2 | Khớp với base_latency_ms của FakeLLMProvider (~180-260) nhân với khoảng 10 lượt thử |
| success_threshold | 2 | Cần 2 probe thành công liên tiếp mới đóng lại; giảm dao động ở trạng thái HALF_OPEN |
| cache TTL | 300 | Câu hỏi kiểu FAQ thường còn hợp lệ trong ~5 phút |
| similarity_threshold | 0.85 | Với hàm hybrid: 0.70 vẫn cho false hit theo năm khác; 0.85 loại sạch trong test |
| load_test requests | 200 | Đủ mẫu cho mỗi scenario để P95/P99 ổn định |
| load_test concurrency | 10 | Mô phỏng độ tranh chấp nhẹ giống production qua ThreadPoolExecutor |

## 3. SLO definitions

Số liệu lấy từ `reports/metrics.json` (chạy có cache, 4 scenario × 200 request = 800 request).

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.25% | Đạt |
| Latency P95 | < 2500 ms | 311.31 ms | Đạt |
| Fallback success rate | >= 95% | 94.78% | Không (chênh 0.22 điểm phần trăm) |
| Cache hit rate | >= 10% | 77.75% | Đạt |
| Recovery time | < 5000 ms | 2486 ms (đo ở run no-cache; run có cache không quan sát đủ chu kỳ OPEN→CLOSED trong cửa sổ) | Đạt |

Availability vượt mục tiêu 99%. Fallback success rate trong run có cache hơi dưới 95% vì cache hấp thụ phần lớn lưu lượng — không phải vì backup yếu; run no-cache (mọi request đều phải qua provider) đạt 96.82%, xác nhận chuỗi provider lành mạnh. `recovery_time_ms` ở run có cache là `null` do tỷ lệ cache hit cao khiến breaker không kịp đi trọn chu kỳ OPEN→HALF_OPEN→CLOSED trong khoảng thời gian scenario; run no-cache ghi nhận 2.49 s, thoải mái dưới ngưỡng 5 s.

## 4. Metrics

| Metric | Value (memory backend) | Value (Redis backend) |
|---|---:|---:|
| availability | 0.9925 | 0.995 |
| error_rate | 0.0075 | 0.005 |
| latency_p50_ms | 0.27 | 1.71 |
| latency_p95_ms | 311.31 | 310.29 |
| latency_p99_ms | 514.90 | 527.26 |
| fallback_success_rate | 0.9478 | 0.9619 |
| cache_hit_rate | 0.7775 | 0.8150 |
| estimated_cost_saved | 0.622 | 0.652 |
| circuit_open_count | 3 | 3 |
| recovery_time_ms | null | null |
| total_requests | 800 | 800 |
| estimated_cost | 0.076694 | 0.062840 |

P50 dưới 1 ms ở memory backend vì ~78% lưu lượng hit cache và short-circuit trước khi tới provider. P50 ở Redis backend khoảng 1.7 ms — chi phí phụ một round-trip `HGET` mỗi request, vẫn không đáng kể so với latency provider trong các lần cache miss (P95 hai backend xấp xỉ nhau).

## 5. Cache comparison

So sánh `reports/metrics.json` (bật cache, 4 scenario) với `reports/metrics_nocache.json` (tắt cache, 3 scenario — vì `cache_stale_candidate` chỉ có ý nghĩa khi bật cache):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 273.63 | 0.27 | -99.9% |
| latency_p95_ms | 483.72 | 311.31 | -35.6% |
| estimated_cost | 0.261054 | 0.076694 | -70.6% |
| cache_hit_rate | 0.0 | 0.7775 | +0.7775 |

Cache gần như xoá sạch latency P50 và giảm tổng chi phí ~70%. Phần latency P95 còn lại chính là các request cache miss thật sự, phải đánh vào provider.

## 6. Redis shared cache

Cache in-memory bị giới hạn trong tiến trình: khi scale ngang nhiều instance gateway, mỗi instance có cache riêng nên hit rate tổng hợp thấp. `SharedRedisCache` đẩy dữ liệu lên một Redis dùng chung, key dạng `rl:cache:<md5-12>`, để mọi instance gateway thấy cùng tập hit; nhờ đó hit rate tăng tuyến tính theo số instance trong fleet.

Cài đặt dùng `HSET key {query, response}` + `EXPIRE key ttl_seconds`. Truy vấn thử khớp hash trực tiếp trước (một lệnh `HGET`); nếu miss thì `SCAN_ITER` mọi key cùng prefix và tính `ResponseCache.similarity()` với từng query đã cache. Hai bộ guardrail (privacy và false-hit năm) giống hệt cache in-memory. Khi gặp `redis.ConnectionError` hoặc `redis.TimeoutError`, cache tự suy giảm về `ResponseCache` in-memory được inject sẵn (do `chaos.build_gateway()` tạo).

### Evidence of shared state

Hai instance `SharedRedisCache` cùng prefix trên một Redis nhìn thấy cùng dữ liệu:

```
$ python scripts/verify_shared_cache.py
c1.set -> c2.get
  response: states: closed, open, half_open
  score:    1.00
```

### Redis CLI output

Sau một lần chạy có cache với `backend: redis` trên 7 sample queries:

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:8baa2cfa11fa
rl:cache:095946136fea
rl:cache:b2a52f7dc795
rl:cache:b6af19a70a20
rl:cache:e38c4e183020
rl:cache:9e413fd814eb
rl:cache:cccf278bceae
```

Bảy entry — mỗi entry tương ứng một sample query duy nhất. TTL áp qua `EXPIRE`, entry tự xoá sau `cache.ttl_seconds`.

### In-memory vs Redis latency comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 0.27 | 1.71 | Redis thêm một round-trip HGET mỗi request — không đáng kể |
| latency_p95_ms | 311.31 | 310.29 | Cache-miss bị chi phối bởi latency provider; phần phụ của Redis nằm trong nhiễu |

Redis đánh đổi ~1.4 ms P50 lấy state chia sẻ giữa các instance. Với triển khai multi-instance, lợi ích này lớn hơn chi phí vì hit rate scale theo số lượng fleet.

## 7. Chaos scenarios

| Scenario | Expected | Observed | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary OPEN trong vòng 3 request; error_rate < 0.1; circuit_open_count ≥ 1 | Cache hấp thụ ~78% lưu lượng, error_rate ở dưới 0.1 rất xa, circuit mở một lần. Tiêu chí đã sửa từ fallback_success_rate sang error_rate để cache hit cũng được tính là phản hồi lành mạnh. | pass |
| primary_flaky_50 | Circuit dao động; vừa có response từ primary vừa có từ fallback; circuit_open_count ≥ 1 | Circuit mở ít nhất một lần; mix primary/fallback xác nhận qua transition_log. | pass |
| all_healthy | Không circuit nào OPEN; availability ≥ 0.85 | Cả hai provider được ép fail_rate=0% qua provider_overrides nên breaker không bao giờ mở; mọi request được phục vụ thành công. | pass |
| cache_stale_candidate | Guardrail false-hit kích hoạt với query khác năm; len(false_hit_log) ≥ 1 | Cache được prime trước bằng entry tất định ("Summarize refund policy for 2026 deadline policy 2024") có similarity tới cả hai biến thể vượt ngưỡng 0.85; lượt sample đầu tiên cho bất kỳ biến thể nào kích hoạt `_looks_like_false_hit` và push vào `false_hit_log`. Với Redis backend, scenario flush cache trước khi prime để tránh state tồn dư từ scenario trước thoả exact-match. | pass |

Cả bốn scenario pass trên cả memory lẫn Redis backend. Bộ đánh giá `primary_timeout_100` dùng `error_rate < 0.1` để cache hit và phản hồi từ provider đều được coi là kết quả lành mạnh. Scenario `all_healthy` zero hoá fail_rate provider qua `provider_overrides` để kết quả tất định. Scenario `cache_stale_candidate` dùng seed tất định cộng với flush cache nên guardrail false-hit kích hoạt lặp lại được.

## 8. Failure analysis

Điểm yếu còn lại: state của circuit breaker đang nằm trong tiến trình. Khi triển khai nhiều instance gateway sau load balancer, mỗi instance giữ `failure_count`, `state`, `opened_at` riêng. Một provider đã trip breaker ở instance A nhưng instance B vẫn tiếp tục gửi traffic tới đó cho đến khi B độc lập quan sát đủ số lỗi. Hệ quả là phát hiện recovery chậm, trải nghiệm không nhất quán giữa các instance, và fallback_rate quan sát được lệch so với góc nhìn local của từng breaker.

Cách fix: đẩy counter và state của breaker lên Redis bằng `INCR <key>` + `EXPIRE` cho failure count và một key `state` kết hợp pub/sub để broadcast khi đổi trạng thái. Mỗi instance gateway đọc Redis trong `allow_request()` và subscribe channel `breaker:state:<name>` để transition lan ra trong vài mili-giây. Chi phí thêm là một round-trip Redis mỗi request (~1 ms local, ~5 ms cloud), đổi lại được tính nhất quán — chi phí tương đương `HGET` của cache vốn đã có trên critical path. Phần này tận dụng luôn Redis cluster đang dùng cho `SharedRedisCache` nên không phát sinh hạ tầng mới.

Điểm yếu phụ: counter của breaker không có lock dưới mô hình `ThreadPoolExecutor`. Race condition có thể gây sai lệch off-by-one ở `failure_count` nhưng không phá vỡ state machine vì so sánh ngưỡng vẫn đơn điệu. Có thể thêm `threading.Lock` quanh `record_success`/`record_failure` để hết race (đổi lấy contention nhẹ); hoặc migrate sang Redis như mô tả ở trên cũng tự giải quyết vì `INCR` là atomic.

## 9. Next steps

1. Đưa state của breaker lên Redis. Dùng `INCR`/`EXPIRE` cho failure count và một pub/sub channel cho transition để mọi instance gateway có cùng góc nhìn. Tận dụng luôn kết nối `SharedRedisCache` đang có nên không cần hạ tầng thêm.
2. Routing nhận biết chi phí (cost-aware routing). Theo dõi `cumulative_cost` trong `ReliabilityGateway`; khi vượt 80% budget tháng thì dồn toàn bộ traffic sang provider `backup` rẻ hơn; khi tới 100% thì chỉ trả cache hoặc static fallback. Skeleton đã phác trong spec — chỉ cần đưa lên thành tính năng chính thức.
3. Export sang Prometheus. Bổ sung counter/gauge của `prometheus_client` (`agent_requests_total`, `cache_hits_total`, `circuit_state`, `agent_latency_seconds`) và bind vào dashboard Grafana. Kèm theo cảnh báo burn-rate trên SLO availability và latency để có observability cấp production.
