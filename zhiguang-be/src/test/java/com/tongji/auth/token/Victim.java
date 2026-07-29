package com.tongji.auth.token;

import java.io.FileInputStream;
import java.io.ObjectInputStream;

public class Victim {
    public static void main(String[] args) throws Exception {
        // 你的程序反序列化了不可信数据
        ObjectInputStream ois = new ObjectInputStream(new FileInputStream("evil.data"));

        // 这一行就中招！
        Object obj = ois.readObject();

        System.out.println("反序列化完成：" + obj);
    }
}
