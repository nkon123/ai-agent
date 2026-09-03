package sample;

// Java 는 SQL 이 문자열 안에 있다. 문자열을 지우면 SQL 자체가 사라진다.
public class OrderLoader {
    public void load() {
        String sql = "SELECT ORD_NO, TOTAL_AMT FROM IF_ORDER_TMP WHERE STATUS = 'N'";
        jdbc.query(sql);

        jdbc.update("UPDATE IF_ORDER_TMP SET STATUS = 'Y' WHERE ORD_NO = ?", ordNo);
    }
}
