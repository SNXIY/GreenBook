package com.tongji.sentive;

import com.github.houbb.sensitive.word.bs.SensitiveWordBs;
import org.junit.jupiter.api.Test;

class SensitiveTest {

    @Test
    void testSensitiveWord() {
        SensitiveWordBs sensitiveWordBs = SensitiveWordBs.newInstance().init();

        String text = "s_b";

        boolean contains = sensitiveWordBs.contains(text);
        String result = sensitiveWordBs.replace(text);

        System.out.println("是否包含敏感词：" + contains);
        System.out.println("过滤后：" + result);
    }
}