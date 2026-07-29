package com.tongji.auth.token;

import java.lang.annotation.*;

// 运行时才能读到
@Retention(RetentionPolicy.RUNTIME)
// 可以放在类上
@Target(ElementType.METHOD)
public @interface MyAnno {
    String value();
}
