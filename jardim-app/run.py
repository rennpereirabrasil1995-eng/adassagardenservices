"""Ponto de entrada. Para rodar no seu computador:  python run.py"""
import os

from jardim import create_app

app = create_app()

if __name__ == "__main__":
    # HOST=0.0.0.0 deixa outros aparelhos da mesma rede Wi-Fi acessarem (bom para testar no celular).
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
