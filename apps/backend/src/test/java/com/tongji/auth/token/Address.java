package com.tongji.auth.token;

import org.springframework.boot.autoconfigure.integration.IntegrationAutoConfiguration;

import java.util.List;

import java.util.ArrayList;
import java.util.List;

public class Address implements Cloneable {
    private int num;
    private String name;
    private List<Integer> list;

    public Address(int num, String name, List<Integer> list) {
        this.num = num;
        this.name = name;
        this.list = list;
    }

    // 深拷贝核心方法
    @Override
    public Address clone() {
        try {
            // 1. 先做浅拷贝（复制基本类型+引用地址）
            Address copy = (Address) super.clone();

            // 2. 手动深拷贝 List（关键！）
            if (this.list != null) {
                copy.list = new ArrayList<>(this.list);
            }

            // 3. 返回完全独立的对象
            return copy;

        } catch (CloneNotSupportedException e) {
            throw new RuntimeException(e);
        }
    }

    // -------------------
    // 以下 getter/setter/toString 不变
    // -------------------
    public Address() {}

    @Override
    public String toString() {
        return name + ":" + num + ":" + list.get(0);
    }

    public int getNum() {return num;}
    public String getName() {return name;}
    public void setNum(int num) {this.num = num;}
    public void setName(String name) {this.name = name;}
    public List<Integer> getList() {return list;}
    public void setList(List<Integer> list) {this.list = list;}
}