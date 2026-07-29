package com.tongji.moderation;

import com.github.houbb.sensitive.word.bs.SensitiveWordBs;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

@Service
public class HoubbSensitiveWordService implements SensitiveWordService {
    private final SensitiveWordBs sensitiveWordBs;

    public HoubbSensitiveWordService() {
        this.sensitiveWordBs = SensitiveWordBs.newInstance().init();
    }

    @Override
    public List<String> findAll(String... texts) {
        if (texts == null) {
            return List.of();
        }
        Set<String> words = new LinkedHashSet<>();
        for (String text : texts) {
            if (text != null && !text.isBlank()) {
                words.addAll(sensitiveWordBs.findAll(text));
            }
        }
        return new ArrayList<>(words);
    }
}
