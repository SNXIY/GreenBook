package com.tongji.auth.token;

import java.io.FileOutputStream;
import java.io.ObjectOutputStream;

public class Hacker {
    public static void main(String[] args) throws Exception {
        Object object1 = new Object();
        new Thread(()->{
            synchronized (object1) {
                System.out.println("获取锁成功");
                try {
                    object1.wait();
                    System.out.println("被唤醒");
                } catch (InterruptedException e) {
                    throw new RuntimeException(e);
                }
            }
        }).start();

        new Thread(()->{
            synchronized (object1) {
                System.out.println("获取锁成功2");
                object1.notify();
                System.out.println("释放锁");
            }
        }).start();
    }
}
