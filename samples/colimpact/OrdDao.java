package sample;

// 컬럼 영향도 평가용 가짜 Java. SQL 이 문자열 안에 있다.
public class OrdDao {
    public void sum() {
        String sql = "SELECT SUM(TOTAL_AMT) FROM IF_ORDER_TMP WHERE STATUS = 'N'";
        jdbc.query(sql);
    }
}
