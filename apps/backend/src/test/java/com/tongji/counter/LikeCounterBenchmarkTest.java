package com.tongji.counter;

import com.tongji.counter.schema.BitmapShard;
import com.tongji.counter.schema.CounterKeys;
import com.tongji.counter.schema.CounterSchema;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.RedisCallback;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.LongAdder;

/**
 * Manual Redis benchmark for the like counter write path.
 *
 * <p>It bypasses HTTP, JWT, Spring Security, and Kafka, then compares only the
 * counter write strategy after bitmap idempotency:
 * <ul>
 *   <li>HASH_AGG: SETBIT + HINCRBY aggregation bucket</li>
 *   <li>DIRECT_SDS: SETBIT + direct SDS byte-segment update</li>
 * </ul>
 *
 * <p>Run explicitly, for example:
 * <pre>
 * mvn -Dtest=LikeCounterBenchmarkTest -Dbench=true test
 * mvn -Dtest=LikeCounterBenchmarkTest -Dbench=true -Dbench.ops=200000 -Dbench.threads=64 test
 * </pre>
 */
@EnabledIfSystemProperty(named = "bench", matches = "true")
class LikeCounterBenchmarkTest {

    private static final String ENTITY_TYPE = prop("bench.entityType", "benchpost");
    private static final int TOTAL_ENTITIES = intProp("bench.entities", 10_000);
    private static final int HOT_ENTITIES = intProp("bench.hotEntities", 100);
    private static final int HOT_PERCENT = intProp("bench.hotPercent", 80);
    private static final int OPS = intProp("bench.ops", 100_000);
    private static final int WARMUP_OPS = intProp("bench.warmupOps", 10_000);
    private static final int THREADS = intProp("bench.threads", Runtime.getRuntime().availableProcessors() * 4);
    private static final boolean INCLUDE_READ_BACK = boolProp("bench.includeReadBack", true);

    private final DefaultRedisScript<Long> hashAggLikeScript = script(HASH_AGG_LIKE_LUA);
    private final DefaultRedisScript<Long> directSdsLikeScript = script(DIRECT_SDS_LIKE_LUA);

    @Test
    void compareHashAggregationAndDirectSds() throws Exception {
        LettuceConnectionFactory factory = redisFactory();
        factory.afterPropertiesSet();
        StringRedisTemplate redis = new StringRedisTemplate(factory);
        redis.afterPropertiesSet();

        try {
            BenchmarkResult hashAgg = runMode(redis, Mode.HASH_AGG);
            BenchmarkResult directSds = runMode(redis, Mode.DIRECT_SDS);

            System.out.println();
            System.out.println("========== LIKE COUNTER BENCHMARK ==========");
            System.out.printf("ops=%d, warmupOps=%d, threads=%d, entities=%d, hotEntities=%d, hotPercent=%d%%%n",
                    OPS, WARMUP_OPS, THREADS, TOTAL_ENTITIES, HOT_ENTITIES, HOT_PERCENT);
            System.out.printf("includeReadBack=%s%n", INCLUDE_READ_BACK);
            hashAgg.print();
            directSds.print();
            System.out.printf("throughput ratio HASH_AGG / DIRECT_SDS = %.2fx%n",
                    hashAgg.throughput() / Math.max(1.0, directSds.throughput()));
            System.out.println("============================================");
        } finally {
            factory.destroy();
        }
    }

    private BenchmarkResult runMode(StringRedisTemplate redis, Mode mode) throws Exception {
        cleanup(redis);
        execute(redis, mode, WARMUP_OPS, 1_000_000_000L, false);
        cleanup(redis);
        return execute(redis, mode, OPS, 2_000_000_000L, true);
    }

    private BenchmarkResult execute(StringRedisTemplate redis, Mode mode, int ops, long uidBase, boolean recordLatency)
            throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(THREADS);
        CountDownLatch latch = new CountDownLatch(THREADS);
        AtomicLong sequence = new AtomicLong();
        LongAdder changed = new LongAdder();
        long[] latencies = recordLatency ? new long[ops] : null;

        long started = System.nanoTime();
        for (int i = 0; i < THREADS; i++) {
            pool.submit(() -> {
                try {
                    while (true) {
                        long n = sequence.getAndIncrement();
                        if (n >= ops) {
                            return;
                        }
                        String entityId = entityId(n);
                        long uid = uidBase + n;
                        long begin = recordLatency ? System.nanoTime() : 0L;
                        Long result = like(redis, mode, entityId, uid);
                        if (INCLUDE_READ_BACK) {
                            isLiked(redis, entityId, uid);
                        }
                        if (recordLatency) {
                            latencies[(int) n] = System.nanoTime() - begin;
                        }
                        if (result != null && result == 1L) {
                            changed.increment();
                        }
                    }
                } finally {
                    latch.countDown();
                }
            });
        }

        latch.await();
        long elapsed = System.nanoTime() - started;
        pool.shutdown();
        return new BenchmarkResult(mode, ops, changed.sum(), elapsed, latencies);
    }

    private Long like(StringRedisTemplate redis, Mode mode, String entityId, long uid) {
        long chunk = BitmapShard.chunkOf(uid);
        long bit = BitmapShard.bitOf(uid);
        String bitmapKey = CounterKeys.bitmapKey("like", ENTITY_TYPE, entityId, chunk);
        String counterKey = mode == Mode.HASH_AGG
                ? CounterKeys.aggKey(ENTITY_TYPE, entityId)
                : CounterKeys.sdsKey(ENTITY_TYPE, entityId);

        if (mode == Mode.HASH_AGG) {
            return redis.execute(hashAggLikeScript, List.of(bitmapKey, counterKey),
                    String.valueOf(bit),
                    String.valueOf(CounterSchema.IDX_LIKE));
        }

        return redis.execute(directSdsLikeScript, List.of(bitmapKey, counterKey),
                String.valueOf(bit),
                String.valueOf(CounterSchema.SCHEMA_LEN),
                String.valueOf(CounterSchema.FIELD_SIZE),
                String.valueOf(CounterSchema.IDX_LIKE));
    }

    private boolean isLiked(StringRedisTemplate redis, String entityId, long uid) {
        long chunk = BitmapShard.chunkOf(uid);
        long bit = BitmapShard.bitOf(uid);
        String bitmapKey = CounterKeys.bitmapKey("like", ENTITY_TYPE, entityId, chunk);
        Boolean value = redis.execute((RedisCallback<Boolean>) connection ->
                connection.stringCommands().getBit(bitmapKey.getBytes(StandardCharsets.UTF_8), bit));
        return Boolean.TRUE.equals(value);
    }

    private void cleanup(StringRedisTemplate redis) {
        List<String> patterns = List.of(
                "bm:like:" + ENTITY_TYPE + ":*",
                "agg:" + CounterSchema.SCHEMA_ID + ":" + ENTITY_TYPE + ":*",
                "cnt:" + CounterSchema.SCHEMA_ID + ":" + ENTITY_TYPE + ":*"
        );
        for (String pattern : patterns) {
            Set<String> keys = redis.keys(pattern);
            if (keys != null && !keys.isEmpty()) {
                redis.delete(keys);
            }
        }
    }

    private static LettuceConnectionFactory redisFactory() {
        RedisStandaloneConfiguration config = new RedisStandaloneConfiguration();
        config.setHostName(prop("bench.redis.host", "127.0.0.1"));
        config.setPort(intProp("bench.redis.port", 6379));
        String password = System.getProperty("bench.redis.password");
        if (password != null && !password.isBlank()) {
            config.setPassword(password);
        }
        config.setDatabase(intProp("bench.redis.database", 0));
        return new LettuceConnectionFactory(config);
    }

    private static String entityId(long n) {
        long x = mix64(n);
        int bucket = (int) Math.floorMod(x, 100);
        if (bucket < HOT_PERCENT) {
            return String.valueOf(1 + Math.floorMod(x >>> 8, HOT_ENTITIES));
        }
        int coldCount = Math.max(1, TOTAL_ENTITIES - HOT_ENTITIES);
        return String.valueOf(1 + HOT_ENTITIES + Math.floorMod(x >>> 8, coldCount));
    }

    private static long mix64(long z) {
        z = (z ^ (z >>> 33)) * 0xff51afd7ed558ccdL;
        z = (z ^ (z >>> 33)) * 0xc4ceb9fe1a85ec53L;
        return z ^ (z >>> 33);
    }

    private static DefaultRedisScript<Long> script(String text) {
        DefaultRedisScript<Long> script = new DefaultRedisScript<>();
        script.setResultType(Long.class);
        script.setScriptText(text);
        return script;
    }

    private static String prop(String name, String fallback) {
        return System.getProperty(name, fallback);
    }

    private static int intProp(String name, int fallback) {
        return Integer.parseInt(System.getProperty(name, String.valueOf(fallback)));
    }

    private static boolean boolProp(String name, boolean fallback) {
        return Boolean.parseBoolean(System.getProperty(name, String.valueOf(fallback)));
    }

    private enum Mode {
        HASH_AGG,
        DIRECT_SDS
    }

    private record BenchmarkResult(Mode mode, int ops, long changed, long elapsedNanos, long[] latencies) {
        double throughput() {
            return ops * 1_000_000_000.0 / elapsedNanos;
        }

        void print() {
            List<Long> sorted = new ArrayList<>(latencies.length);
            for (long latency : latencies) {
                sorted.add(latency);
            }
            sorted.sort(Long::compareTo);
            System.out.printf("%s: throughput=%.0f ops/s, elapsed=%.3fs, changed=%d, avg=%.3fms, p95=%.3fms, p99=%.3fms%n",
                    mode,
                    throughput(),
                    elapsedNanos / 1_000_000_000.0,
                    changed,
                    avgMillis(sorted),
                    percentileMillis(sorted, 0.95),
                    percentileMillis(sorted, 0.99));
        }

        private static double avgMillis(List<Long> sorted) {
            long sum = 0L;
            for (Long value : sorted) {
                sum += value;
            }
            return sum / (double) sorted.size() / 1_000_000.0;
        }

        private static double percentileMillis(List<Long> sorted, double percentile) {
            int index = Math.min(sorted.size() - 1, Math.max(0, (int) Math.ceil(sorted.size() * percentile) - 1));
            return sorted.get(index) / 1_000_000.0;
        }
    }

    private static final String HASH_AGG_LIKE_LUA = """
            local bmKey = KEYS[1]
            local aggKey = KEYS[2]
            local offset = tonumber(ARGV[1])
            local idx = ARGV[2]
            local prev = redis.call('GETBIT', bmKey, offset)
            if prev == 1 then return 0 end
            redis.call('SETBIT', bmKey, offset, 1)
            redis.call('HINCRBY', aggKey, idx, 1)
            return 1
            """;

    private static final String DIRECT_SDS_LIKE_LUA = """
            local bmKey = KEYS[1]
            local cntKey = KEYS[2]
            local offset = tonumber(ARGV[1])
            local schemaLen = tonumber(ARGV[2])
            local fieldSize = tonumber(ARGV[3])
            local idx = tonumber(ARGV[4])

            local prev = redis.call('GETBIT', bmKey, offset)
            if prev == 1 then return 0 end
            redis.call('SETBIT', bmKey, offset, 1)

            local function read32be(s, off)
              local b = {string.byte(s, off + 1, off + 4)}
              local n = 0
              for i = 1, 4 do n = n * 256 + b[i] end
              return n
            end

            local function write32be(n)
              local t = {}
              for i = 4, 1, -1 do
                t[i] = n % 256
                n = math.floor(n / 256)
              end
              return string.char(unpack(t))
            end

            local cnt = redis.call('GET', cntKey)
            if not cnt then cnt = string.rep(string.char(0), schemaLen * fieldSize) end
            local off = idx * fieldSize
            local v = read32be(cnt, off) + 1
            local seg = write32be(v)
            cnt = string.sub(cnt, 1, off) .. seg .. string.sub(cnt, off + fieldSize + 1)
            redis.call('SET', cntKey, cnt)
            return 1
            """;
}
