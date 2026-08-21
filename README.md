**Beyond Food Security — Food System Resilience Agent**

This project is being developed for the **Women in Data Datathon 2026: What’s Cooking? From Farm to Fork**.

The goal is to identify countries that may appear food-secure today but have hidden structural vulnerabilities caused by:

* low crop diversity
* high import dependency
* concentrated food suppliers
* unstable production
* food affordability and access gaps

The project introduces the concept of a **Latent Vulnerability Gap**, comparing:

**Current Food Security**
vs.
**Structural Food System Resilience**

The system will use FAOSTAT production, trade, food balance, food security, and healthy-diet affordability datasets to:

**Detect → Explain → Stress-test → Recommend**

Example scenario:

> *What happens if Australian wheat production decreases by 15%?*

The system analyses trade dependencies, supplier concentration, domestic production capacity, and existing food-security conditions to identify countries with the greatest exposure.

**Planned stack:** Python, Pandas, NumPy, GeoPandas, NetworkX, Plotly, FastAPI/Streamlit, FAOSTAT data, and Claude for evidence-grounded AI analysis.

**Research focus:**
*Which countries appear food-secure today but contain production and trade dependencies that could make them vulnerable to future food-system shocks?*
