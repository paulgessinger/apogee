from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired, InputRequired


class ValidationCreateForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired()])


class PatchForm(FlaskForm):
    url = StringField(
        "Name", validators=[InputRequired()], render_kw={"placeholder": "URL"}
    )
