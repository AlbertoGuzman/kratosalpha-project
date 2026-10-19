import pandas as pd
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

# 1. Cargar el archivo CSV de backtest usando una cadena raw ('r')
csv_path = r'C:\alberto\workspace\kratosalpha-project\trading_system\exports\connors_20100101_20261019_nolookahead_fill_sim.csv'
df = pd.read_csv(csv_path)

# Limpiar valores nulos por seguridad
df = df.dropna(subset=['net_pnl'])

# 2. Definir la variable objetivo (1 si ganó dinero, 0 si perdió)
df['target'] = (df['net_pnl'] > 0).astype(int)

# 3. Seleccionar características (features) para el modelo
features = ['connors_rsi_entrada', 'rsi3_entrada', 'streak_entrada']
X = df[features]
y = df['target']

# 4. Entrenar un Árbol de Decisión con mayor profundidad (max_depth=5)
tree_model = DecisionTreeClassifier(max_depth=5, random_state=42)
tree_model.fit(X, y)

# Calcular precisión del modelo sobre los propios datos
predictions = tree_model.predict(X)
acc = accuracy_score(y, predictions)
print(f"--- PRECISIÓN DEL ÁRBOL EN LOS DATOS: {acc * 100:.2f}% ---\n")

# 5. Imprimir las reglas lógicas ampliadas
print("--- REGLAS AMPLIADAS DEL ÁRBOL DE DECISIÓN (max_depth=5) ---")
tree_rules = export_text(tree_model, feature_names=features)
print(tree_rules)

# 6. Mostrar la importancia detallada de cada variable
print("\n--- IMPORTANCIA DE LAS VARIABLES ---")
importances = pd.DataFrame({
    'Variable': features,
    'Importancia': tree_model.feature_importances_
}).sort_values(by='Importancia', ascending=False)
print(importances)