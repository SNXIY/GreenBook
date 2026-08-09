package com.tongji.auth.token;

public class User {

    // 静态变量（属于类 → 方法区/Class对象）
    public static int staticAge = 18;

    // 实例变量（属于对象 → 堆）
    public String name;
    public int age;

    // 静态代码块 → 类初始化时执行
    static {
        System.out.println("类初始化了");
    }

    // 构造方法 → new 对象时执行
    public User(String name, int age) {
        this.name = name;
        this.age = age;
    }
}
