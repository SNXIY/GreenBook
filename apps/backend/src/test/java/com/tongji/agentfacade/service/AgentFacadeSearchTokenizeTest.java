package com.tongji.agentfacade.service;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Unit tests for the zero-dependency search query tokenizer. */
class AgentFacadeSearchTokenizeTest {

    @Test
    void splitsSpaceSeparatedPhrase() {
        assertEquals(List.of("java", "后端", "面试"),
                AgentFacadeService.tokenizeSearchQuery("Java 后端 面试"));
    }

    @Test
    void splitsCjkLatinBoundaryWithoutSpace() {
        assertEquals(List.of("java", "后端"),
                AgentFacadeService.tokenizeSearchQuery("Java后端"));
    }

    @Test
    void dropsStopWordsAndSingleCjkChars() {
        assertEquals(List.of("java", "后端", "面试"),
                AgentFacadeService.tokenizeSearchQuery("搜索最近比较热门的 Java 后端 面试 帖 子"));
    }

    @Test
    void normalizesFullWidthAndPunctuation() {
        assertEquals(List.of("java", "面试"),
                AgentFacadeService.tokenizeSearchQuery("Ｊａｖａ，面试！"));
    }

    @Test
    void keepsLatinNumbersAndTerms() {
        assertEquals(List.of("面试题", "10"),
                AgentFacadeService.tokenizeSearchQuery("面试题 10 个"));
    }

    @Test
    void emptyAndBlankReturnEmpty() {
        assertTrue(AgentFacadeService.tokenizeSearchQuery(null).isEmpty());
        assertTrue(AgentFacadeService.tokenizeSearchQuery("   ").isEmpty());
    }

    @Test
    void singleEnglishTermSurvives() {
        assertEquals(List.of("java"),
                AgentFacadeService.tokenizeSearchQuery("Java"));
    }
}
