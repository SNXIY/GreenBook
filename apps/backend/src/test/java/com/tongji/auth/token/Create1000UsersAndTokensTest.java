package com.tongji.auth.token;

import com.tongji.user.domain.User;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Disabled;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.security.crypto.password.PasswordEncoder;
import javax.sql.DataSource;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;

@SpringBootTest
@TestConfiguration
@Disabled("Manual load-data generator; never run as part of the automated test suite")
public class Create1000UsersAndTokensTest {

    @Autowired
    DataSource dataSource;

    @Autowired
    PasswordEncoder passwordEncoder;

    @Autowired
    JwtService jwtService; // 你项目里的 JWT 服务

    // 固定密码，方便你以后登录
    private final String FIXED_PASSWORD = "123456";

//    @Test
//    public void create1000UsersAndGenerateTokens() throws Exception {
//        String FIXED_PASSWORD = "123456";
//        String passwordHash = passwordEncoder.encode(FIXED_PASSWORD);
//
//        try (Connection conn = dataSource.getConnection()) {
//            String sql = "INSERT INTO users (" +
//                    "phone, email, password_hash, nickname, avatar, bio, zg_id, gender, birthday, school, tags_json" +
//                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";
//
//            conn.setAutoCommit(false);
//            try (PreparedStatement pstmt = conn.prepareStatement(sql)) {
//                for (int i = 1; i <= 10000; i++) {
//                    pstmt.setString(1, "1380000" + String.format("%04d", i));
//                    pstmt.setString(2, "user" + i + "@test.com");
//                    pstmt.setString(3, passwordHash);
//                    pstmt.setString(4, "压测用户" + i);
//                    pstmt.setString(5, null);
//                    pstmt.setString(6, "专注压测");
//                    pstmt.setString(7, "ZG" + String.format("%06d", i));
//                    pstmt.setString(8, "UNKNOWN");
//                    pstmt.setDate(9, null);
//                    pstmt.setString(10, "压测大学");
//                    pstmt.setString(11, "[]");
//                    pstmt.addBatch();
//                }
//                pstmt.executeBatch();
//                conn.commit();
//                System.out.println("✅ 1000 个用户插入成功！");
//            }
//        }
//
//        // ====================== 修复版本生成 Token ======================
//        try (FileWriter writer = new FileWriter("D:/JAVA/zhiguang_be-main (1)/zhiguang_be-main/tokens.csv")) {
//            for (int i = 1; i <= 10000; i++) {
//                Long userId = (long) i;
//                String phone = "1380000" + String.format("%04d", i);
//
//                // 构造完整 User 对象（满足 JWT 生成要求）
//                User user = new User();
//                user.setId(userId);
//                user.setPhone(phone);
//                user.setNickname("压测用户" + i);
//
//                // 生成真实有效 Token
//                TokenPair tokenPair = jwtService.issueTokenPair(user);
//                String accessToken = tokenPair.accessToken();
//
//                writer.write(accessToken + "\n");
//                System.out.println("用户ID: " + userId + " token: " + accessToken);
//            }
//        }
//
//        System.out.println("\n🎉 1000 个 Token 生成完成 → tokens.csv");
//    }

    @Test
    public void create1000UsersAndGenerateToken() throws Exception {
        String path = "D:/JAVA/zhiguang_be-main (1)/zhiguang_be-main/tokens.csv";

        List<String> tokens = new ArrayList<>();
        for (int i = 1; i <= 10000; i++) {
            User user = new User();
            user.setId((long) i);
            user.setPhone("1380000" + String.format("%04d", i));
            user.setNickname("压测用户" + i);

            TokenPair tokenPair = jwtService.issueTokenPair(user);
            tokens.add(tokenPair.accessToken());
        }

        // 一次性写入，保证最后一行没有多余换行
        Files.write(Paths.get(path), tokens, StandardCharsets.UTF_8);

        long lines = Files.lines(Paths.get(path)).count();
        System.out.println("✅ 最终行数：" + lines); // 必须 1000
    }
}
