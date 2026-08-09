package com.tongji.relation.outbox;

import com.alibaba.otter.canal.client.CanalConnector;
import com.alibaba.otter.canal.client.CanalConnectors;
import com.alibaba.otter.canal.protocol.CanalEntry;
import com.alibaba.otter.canal.protocol.Message;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.SmartLifecycle;
import org.springframework.core.task.TaskExecutor;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;

import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Bridges new outbox rows from Canal to Kafka.
 *
 * <p>Only INSERT rows with status=NEW are published. Status updates produced by this
 * bridge are ignored so they do not create a publish loop through binlog.</p>
 */
@Service
public class CanalKafkaBridge implements SmartLifecycle {
    private static final Logger log = LoggerFactory.getLogger(CanalKafkaBridge.class);

    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper objectMapper;
    private final OutboxMapper outboxMapper;
    private final TaskExecutor taskExecutor;
    private final boolean enabled;
    private final String host;
    private final int port;
    private final String destination;
    private final String username;
    private final String password;
    private final String filter;
    private final int batchSize;
    private final long intervalMs;

    private volatile boolean running;
    private CanalConnector connector;

    public CanalKafkaBridge(KafkaTemplate<String, String> kafka,
                            ObjectMapper objectMapper,
                            OutboxMapper outboxMapper,
                            @Qualifier("taskExecutor") TaskExecutor taskExecutor,
                            @Value("${canal.enabled}") boolean enabled,
                            @Value("${canal.host}") String host,
                            @Value("${canal.port}") int port,
                            @Value("${canal.destination}") String destination,
                            @Value("${canal.username}") String username,
                            @Value("${canal.password}") String password,
                            @Value("${canal.filter}") String filter,
                            @Value("${canal.batchSize}") int batchSize,
                            @Value("${canal.intervalMs}") long intervalMs) {
        this.kafka = kafka;
        this.objectMapper = objectMapper;
        this.outboxMapper = outboxMapper;
        this.taskExecutor = taskExecutor;
        this.enabled = enabled;
        this.host = host;
        this.port = port;
        this.destination = destination;
        this.username = username;
        this.password = password;
        this.filter = filter;
        this.batchSize = batchSize;
        this.intervalMs = intervalMs;
    }

    @Override
    public void start() {
        if (running) {
            return;
        }
        if (!enabled) {
            log.info("Canal bridge disabled");
            return;
        }

        running = true;
        taskExecutor.execute(this::runLoop);
    }

    private void runLoop() {
        try {
            connector = CanalConnectors.newSingleConnector(
                    new InetSocketAddress(host, port), destination, username, password);
            log.info("Canal connecting to {}:{} dest={} filter={}", host, port, destination, filter);
            connector.connect();
            connector.subscribe(filter);
            connector.rollback();

            while (running) {
                Message message = connector.getWithoutAck(batchSize);
                long batchId = message.getId();
                if (batchId == -1 || message.getEntries() == null || message.getEntries().isEmpty()) {
                    sleepInterval();
                    continue;
                }

                boolean delivered = true;
                for (CanalEntry.Entry entry : message.getEntries()) {
                    if (!deliverEntry(entry)) {
                        delivered = false;
                        break;
                    }
                }

                if (delivered) {
                    connector.ack(batchId);
                } else {
                    connector.rollback(batchId);
                    sleepInterval();
                }
            }
        } catch (Exception e) {
            log.error("Canal bridge error", e);
        } finally {
            disconnect();
            running = false;
        }
    }

    private boolean deliverEntry(CanalEntry.Entry entry) {
        if (entry.getEntryType() != CanalEntry.EntryType.ROWDATA) {
            return true;
        }

        CanalEntry.RowChange rowChange;
        try {
            rowChange = CanalEntry.RowChange.parseFrom(entry.getStoreValue());
        } catch (Exception e) {
            log.warn("Canal row change parse failed", e);
            return true;
        }

        if (rowChange.getEventType() != CanalEntry.EventType.INSERT) {
            return true;
        }

        ArrayNode dataArray = objectMapper.createArrayNode();
        List<Long> outboxIds = new ArrayList<>();
        for (CanalEntry.RowData rowData : rowChange.getRowDatasList()) {
            ObjectNode rowNode = objectMapper.createObjectNode();
            Long id = null;
            String payload = null;
            String status = "NEW";

            for (CanalEntry.Column col : rowData.getAfterColumnsList()) {
                String name = col.getName();
                if ("id".equalsIgnoreCase(name)) {
                    try {
                        id = Long.valueOf(col.getValue());
                        rowNode.put("id", col.getValue());
                    } catch (NumberFormatException ignored) {
                    }
                } else if ("payload".equalsIgnoreCase(name)) {
                    payload = col.getValue();
                    rowNode.put("payload", payload);
                } else if ("aggregate_type".equalsIgnoreCase(name)) {
                    rowNode.put("aggregate_type", col.getValue());
                } else if ("aggregate_id".equalsIgnoreCase(name)) {
                    rowNode.put("aggregate_id", col.getValue());
                } else if ("status".equalsIgnoreCase(name)) {
                    status = col.getValue();
                    rowNode.put("status", status);
                } else if ("type".equalsIgnoreCase(name)) {
                    rowNode.put("type", col.getValue());
                }
            }

            if (id != null && payload != null && "NEW".equals(status)) {
                outboxIds.add(id);
                dataArray.add(rowNode);
            }
        }

        if (outboxIds.isEmpty()) {
            return true;
        }

        ObjectNode msgNode = objectMapper.createObjectNode();
        msgNode.put("table", entry.getHeader().getTableName());
        msgNode.put("type", "INSERT");
        msgNode.set("data", dataArray);

        try {
            String json = objectMapper.writeValueAsString(msgNode);
            kafka.send(OutboxTopics.CANAL_OUTBOX, json).get(10, TimeUnit.SECONDS);
            try {
                outboxMapper.markPublished(outboxIds);
            } catch (Exception e) {
                log.warn("Outbox markPublished failed, ids={}", outboxIds, e);
            }
            return true;
        } catch (Exception e) {
            log.error("Outbox delivery to Kafka failed, ids={}", outboxIds, e);
            return false;
        }
    }

    private void sleepInterval() {
        try {
            Thread.sleep(intervalMs);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            running = false;
        }
    }

    private void disconnect() {
        if (connector == null) {
            return;
        }
        try {
            connector.disconnect();
            log.info("Canal disconnected: dest={}", destination);
        } catch (Exception e) {
            log.warn("Canal disconnect failed: dest={} err={}", destination, e.getMessage());
        }
    }

    @Override
    public void stop() {
        running = false;
    }

    @Override
    public boolean isRunning() {
        return running;
    }
}
