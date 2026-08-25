package com.tongji.search.projection;

import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.springframework.boot.autoconfigure.kafka.KafkaProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.config.ConcurrentKafkaListenerContainerFactory;
import org.springframework.kafka.core.DefaultKafkaConsumerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.listener.ContainerProperties;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.kafka.listener.DeadLetterPublishingRecoverer;
import org.springframework.util.backoff.FixedBackOff;

@Configuration
public class SearchProjectionKafkaConfig {
    @Bean(name = "searchProjectionKafkaListenerContainerFactory")
    public ConcurrentKafkaListenerContainerFactory<String, String> searchProjectionKafkaListenerContainerFactory(
            KafkaProperties properties, KafkaTemplate<String, String> kafka) {
        var consumerProperties = properties.buildConsumerProperties();
        consumerProperties.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);
        var consumerFactory = new DefaultKafkaConsumerFactory<String, String>(
                consumerProperties, new StringDeserializer(), new StringDeserializer());
        var factory = new ConcurrentKafkaListenerContainerFactory<String, String>();
        factory.setConsumerFactory(consumerFactory);
        factory.getContainerProperties().setAckMode(ContainerProperties.AckMode.MANUAL);
        DeadLetterPublishingRecoverer recoverer = new DeadLetterPublishingRecoverer(
                kafka,
                (record, exception) -> new TopicPartition(record.topic() + ".search-dlt", record.partition()));
        factory.setCommonErrorHandler(new DefaultErrorHandler(recoverer, new FixedBackOff(1000L, 2L)));
        return factory;
    }
}
