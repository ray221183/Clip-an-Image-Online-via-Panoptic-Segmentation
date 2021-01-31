from flask_ngrok import run_with_ngrok
from flask import Flask, render_template, url_for
import os
from flask import Flask, flash, request, redirect, url_for
from werkzeug.utils import secure_filename
import json
import numpy as np

from panoptic_eval import split_img

UPLOAD_FOLDER = './static/images'
SAVE_FOLDER = './static/postImages'
SAVE_SELREGION_FOLDER = './static/selectedRegion'
SAVE_ID = './static/idList'
ALLOWED_EXTENSIONS = set(['txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'])

app = Flask(__name__)
app.secret_key = 'super secret key'
app.config['SESSION_TYPE'] = 'filesystem'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SAVE_FOLDER'] = SAVE_FOLDER
app.config['SAVE_SELREGION_FOLDER'] = SAVE_SELREGION_FOLDER
app.config['SAVE_ID'] = SAVE_ID
run_with_ngrok(app)   #starts ngrok when the app is run

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/")
def index():
    return render_template("index.html")

def clipImage(data):
    return render_template("clip.html", data=data)

@app.route("/upload", methods=['POST'])
def upload():
    # check if the post request has the file part
    if 'file' not in request.files:
        flash('No file part')
        return 'No file part'
    file = request.files['file']
    # if user does not select file, browser also
    # submit an empty part without filename
    if file.filename == '':
        flash('No selected file')
        return 'No selected file'
    print("filename ", file.filename)
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filename_pre, file_extension = os.path.splitext(filename)
        print(filename_pre)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        img_postimg_path = os.path.join(app.config['UPLOAD_FOLDER'], filename) + ":" + os.path.join(app.config['SAVE_FOLDER'], filename) + ":" + os.path.join(app.config['SAVE_SELREGION_FOLDER'], filename_pre) + ":" + os.path.join(app.config['SAVE_ID'], filename_pre)
        pan_segm_id = split_img(img_postimg_path)
        json_dumps = json.dumps(filename_pre)
        return clipImage(json_dumps)
    return render_template("index.html")
if __name__ == "__main__":
    app.run()
