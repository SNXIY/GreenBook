package com.tongji.storage;

import com.tongji.common.exception.BusinessException;
import com.tongji.storage.config.OssProperties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class OssStorageServiceLocalTest {

    @TempDir
    Path storageRoot;

    @Test
    void storesAndReadsTextWithoutCloudCredentials() {
        OssProperties properties = new OssProperties();
        properties.setProvider("local");
        properties.setLocalRoot(storageRoot.toString());
        properties.setLocalPublicBaseUrl("http://localhost:8080");
        OssStorageService service = new OssStorageService(properties);

        service.putTextObject("knowposts/1/content.md", "正文", "text/markdown");

        assertEquals("正文", service.readTextObject("knowposts/1/content.md", 1024));
        assertEquals(
                "http://localhost:8080/api/v1/storage/files/posts/1/image.png",
                service.publicObjectUrl("posts/1/image.png")
        );
    }

    @Test
    void rejectsPathsOutsideStorageRoot() {
        OssProperties properties = new OssProperties();
        properties.setProvider("local");
        properties.setLocalRoot(storageRoot.toString());
        OssStorageService service = new OssStorageService(properties);

        assertThrows(
                BusinessException.class,
                () -> service.putLocalObject("../outside.txt", new byte[]{1})
        );
    }

    @Test
    void localUploadReturnsStableSha256Etag() {
        OssProperties properties = new OssProperties();
        properties.setProvider("local");
        properties.setLocalRoot(storageRoot.toString());
        OssStorageService service = new OssStorageService(properties);

        String etag = service.putLocalObject(
                "posts/1/content.md",
                "正文".getBytes(StandardCharsets.UTF_8)
        );

        assertEquals(
                "d661c3d96d53ebc0ca8a55aae24b5df4a4d1bf28d37337b982fe8ebf54846eeb",
                etag
        );
    }
}
