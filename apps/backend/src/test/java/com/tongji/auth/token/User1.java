package com.tongji.auth.token;

public class User1 {
    private String name;
    private transient int age;

    // 必须有无参构造（JSON需要）
    public User1() {}

    public User1(String name, int age) {
        this.name = name;
        this.age = age;
    }

    // getter/setter
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public int getAge() { return age; }
    public void setAge(int age) { this.age = age; }

    @Override
    public String toString() {
        return "User{name='"+name+"', age="+age+"}";
    }
}
