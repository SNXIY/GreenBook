package com.tongji.auth.token;

import org.redisson.api.queue.event.AddedEventListener;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Stack;

public class cloneTest {
    public static void main(String[] args) throws CloneNotSupportedException {
        User user = new User("zhiguang", 23);
        System.out.println(User.staticAge);
    }
    public static String largestNumber(int[] nums) {
        int n = nums.length;

        // 将整数转换为字符串数组
        String[] strNums = new String[n];
        for (int i = 0; i < n; i++) {
            strNums[i] = String.valueOf(nums[i]);
        }

        // 自定义排序
        Arrays.sort(strNums, (a, b) -> {
            String order1 = a + b;  // ab
            String order2 = b + a;  // ba
            return order2.compareTo(order1);  // 降序排列
        });

        // 处理前导0的情况
        if (strNums[0].equals("0")) {
            return "0";
        }

        // 拼接结果
        StringBuilder sb = new StringBuilder();
        for (String num : strNums) {
            sb.append(num);
        }

        return sb.toString();
    }
    public static List<Integer> findAllPeaks(int[] nums) {
        List<Integer> res = new ArrayList<>();
        int n = nums.length;
        if (n == 1) {
            res.add(0);
            return res;
        }

        // 最左边
        if (nums[0] > nums[1]) res.add(0);
        // 最右边
        if (nums[n-1] > nums[n-2]) res.add(n-1);
        // 中间
        for (int i = 1; i < n-1; i++) {
            if (nums[i] > nums[i-1] && nums[i] > nums[i+1]) {
                res.add(i);
            }
        }
        return res;
    }
    public static int calculate(String s) {
        Stack<Integer> stack = new Stack<>();
        int num = 0;
        char prevOp = '+'; // 上一个运算符，初始为+
        int n = s.length();

        for (int i = 0; i < n; i++) {
            char c = s.charAt(i);

            // 1. 是数字，拼数
            if (Character.isDigit(c)) {
                num = num * 10 + (c - '0');
            }

            // 2. 是运算符 或 到最后一位，开始计算
            if ((!Character.isDigit(c) && c != ' ') || i == n - 1) {
                switch (prevOp) {
                    case '+':
                        stack.push(num);
                        break;
                    case '-':
                        stack.push(-num);
                        break;
                    case '*':
                        stack.push(stack.pop() * num);
                        break;
                    case '/':
                        // 向0取整，不能直接用Math.divideExact
                        stack.push(stack.pop() / num);
                        break;
                }
                prevOp = c; // 更新运算符
                num = 0;    // 重置当前数字
            }
        }

        // 栈里全部相加
        int sum = 0;
        while (!stack.isEmpty()) {
            sum += stack.pop();
        }
        return sum;
    }
}
