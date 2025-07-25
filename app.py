# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField, FileField
from wtforms.validators import DataRequired, Length
from datetime import datetime
import os
import subprocess
import tempfile
import json
from werkzeug.utils import secure_filename
import sqlite3

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tasks.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'solutions'
app.config['ALLOWED_EXTENSIONS'] = {'py'}


@app.template_filter('fromjson')
def fromjson_filter(data):
    try:
        return json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return {}


db = SQLAlchemy(app)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    solution_code = db.Column(db.Text, nullable=False)
    test_cases = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    solutions = db.relationship('Solution', backref='task', lazy=True, cascade='all, delete-orphan')


class Solution(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    user_id = db.Column(db.String(50), nullable=False)
    filename = db.Column(db.String(100), nullable=False)
    user_code = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')
    test_results = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_latest = db.Column(db.Boolean, default=True)


class TaskForm(FlaskForm):
    title = StringField('Название', validators=[DataRequired()],
                        render_kw={"class": "form-control", "placeholder": "Введите название задачи"})
    description = TextAreaField('Описание', validators=[DataRequired()],
                                render_kw={"class": "form-control", "rows": 3, "placeholder": "Опишите задачу"})
    solution_code = TextAreaField('Эталонное решение', validators=[DataRequired()],
                                  render_kw={"class": "form-control code-editor", "rows": 10,
                                             "placeholder": "Введите правильное решение"})
    test_cases = TextAreaField('Тест-кейсы (JSON)', validators=[DataRequired()],
                               render_kw={"class": "form-control", "rows": 5,
                                          "placeholder": "Введите тест-кейсы в формате JSON"})
    submit = SubmitField('Сохранить', render_kw={"class": "btn btn-primary"})


class SolutionForm(FlaskForm):
    solution_file = FileField('Файл решения (.py)', render_kw={"class": "form-control"})
    solution_code = TextAreaField('Или введите код прямо здесь',
                                  render_kw={"class": "form-control code-editor", "rows": 10})
    submit = SubmitField('Отправить', render_kw={"class": "btn btn-success"})


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def validate_test_cases(json_str):
    try:
        cases = json.loads(json_str)
        if not isinstance(cases, list):
            return False, "Тест-кейсы должны быть массивом"
        return True, ""
    except json.JSONDecodeError:
        return False, "Неверный формат JSON"


def check_and_add_columns():
    db_path = os.path.join(app.instance_path, 'tasks.db')
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(solution)")
        columns = [column[1] for column in cursor.fetchall()]

        if 'user_id' not in columns:
            cursor.execute("ALTER TABLE solution ADD COLUMN user_id VARCHAR(50) DEFAULT 'anonymous'")

        if 'is_latest' not in columns:
            cursor.execute("ALTER TABLE solution ADD COLUMN is_latest BOOLEAN DEFAULT TRUE")

        conn.commit()
    finally:
        conn.close()


def run_code_with_input(user_code, input_val):
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.py', encoding='utf-8', delete=False) as f:
        f.write(user_code)
        temp_path = f.name

    try:
        process = subprocess.run(
            ['python', temp_path],
            input=str(input_val),
            text=True,
            capture_output=True,
            timeout=5
        )
        return process.stdout.strip(), process.stderr.strip()
    finally:
        os.unlink(temp_path)


def check_solution(user_code, task):
    try:
        test_cases = json.loads(task.test_cases)
    except json.JSONDecodeError:
        return {'error': 'Неверный формат тест-кейсов'}

    results = {'passed': 0, 'failed': 0, 'details': []}

    for case in test_cases:
        try:
            input_val = case['input'][0]
            expected = str(case['expected'])

            if case.get('expect_exception', False):
                try:
                    output, error = run_code_with_input(user_code, input_val)
                    passed = False
                    received = error if error else "No exception raised"
                except subprocess.CalledProcessError as e:
                    passed = expected in e.stderr
                    received = e.stderr.strip()
            else:
                output, error = run_code_with_input(user_code, input_val)
                passed = output == expected
                received = output

            results['details'].append({
                'input': input_val,
                'expected': expected,
                'received': received,
                'passed': passed
            })
        except Exception as e:
            results['details'].append({
                'input': case['input'],
                'error': str(e),
                'passed': False
            })

    results['passed'] = sum(1 for d in results['details'] if d.get('passed', False))
    results['failed'] = len(results['details']) - results['passed']
    return results


@app.route('/')
def index():
    form = SolutionForm()
    tasks = Task.query.order_by(Task.created_at.desc()).all()
    return render_template('index.html', form=form, tasks=tasks)


@app.route('/task/new', methods=['GET', 'POST'])
def new_task():
    form = TaskForm()
    if form.validate_on_submit():
        is_valid, error_msg = validate_test_cases(form.test_cases.data)
        if not is_valid:
            flash(f'Ошибка в тест-кейсах: {error_msg}', 'danger')
        else:
            task = Task(
                title=form.title.data,
                description=form.description.data,
                solution_code=form.solution_code.data,
                test_cases=form.test_cases.data
            )
            db.session.add(task)
            db.session.commit()
            flash('Задача создана!', 'success')
            return redirect(url_for('index'))
    return render_template('task_form.html', form=form)


@app.route('/task/<int:task_id>', methods=['GET', 'POST'])
def task_detail(task_id):
    task = Task.query.get_or_404(task_id)
    form = SolutionForm()
    user_id = "user1"

    if form.validate_on_submit():
        user_code = ""
        filename = ""

        if form.solution_file.data:
            file = form.solution_file.data
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                with open(filepath, 'r', encoding='utf-8') as f:
                    user_code = f.read()
        elif form.solution_code.data:
            user_code = form.solution_code.data
            filename = "direct_input.py"

        if user_code:
            if "input(" not in user_code or "print(" not in user_code:
                flash('Решение должно использовать input() для ввода и print() для вывода', 'danger')
                return redirect(url_for('task_detail', task_id=task.id))

            test_results = check_solution(user_code, task)

            Solution.query.filter_by(task_id=task.id, user_id=user_id).update({'is_latest': False})

            solution = Solution(
                task_id=task.id,
                user_id=user_id,
                filename=filename,
                user_code=user_code,
                status='checked',
                test_results=json.dumps(test_results, ensure_ascii=False),
                is_latest=True
            )

            db.session.add(solution)
            db.session.commit()

            if 'error' in test_results:
                flash(test_results['error'], 'danger')
            else:
                flash(f'Пройдено тестов: {test_results["passed"]}/{len(test_results["details"])}',
                      'success' if test_results["passed"] == len(test_results["details"]) else 'warning')

    solutions = Solution.query.filter_by(task_id=task.id, is_latest=True).order_by(Solution.created_at.desc()).all()
    return render_template('task_detail.html', task=task, form=form, solutions=solutions)


@app.route('/task/delete/<int:task_id>', methods=['POST'])
def delete_task(task_id):
    task = Task.query.get_or_404(task_id)
    db.session.delete(task)
    db.session.commit()
    flash('Задача удалена!', 'success')
    return redirect(url_for('index'))


@app.route('/solution/delete/<int:solution_id>', methods=['POST'])
def delete_solution(solution_id):
    solution = Solution.query.get_or_404(solution_id)
    task_id = solution.task_id
    db.session.delete(solution)
    db.session.commit()
    flash('Решение удалено!', 'success')
    return redirect(url_for('task_detail', task_id=task_id))


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


def init_db():
    with app.app_context():
        os.makedirs(app.instance_path, exist_ok=True)
        db.create_all()
        check_and_add_columns()

        if not Task.query.first():
            task = Task(
                title="Факториал числа",
                description="Напишите функцию factorial(n), которая вычисляет факториал целого неотрицательного числа n. Используйте input() для ввода и print() для вывода.",
                solution_code="n = int(input())\nif n < 0:\n    print(\"ValueError\")\nelse:\n    result = 1\n    for i in range(1, n + 1):\n        result *= i\n    print(result)",
                test_cases=json.dumps([
                    {"input": [0], "expected": "1"},
                    {"input": [1], "expected": "1"},
                    {"input": [5], "expected": "120"},
                    {"input": [-1], "expected": "ValueError", "expect_exception": True}
                ], ensure_ascii=False)
            )
            db.session.add(task)

            sum_task = Task(
                title="Сумма двух чисел",
                description="Напишите программу, которая принимает два числа и выводит их сумму. Используйте input() для ввода и print() для вывода.",
                solution_code="a = int(input())\nb = int(input())\nprint(a + b)",
                test_cases=json.dumps([
                    {"input": [5, 3], "expected": "8"},
                    {"input": [0, 0], "expected": "0"},
                    {"input": [-1, 1], "expected": "0"},
                    {"input": [100, 200], "expected": "300"}
                ], ensure_ascii=False)
            )
            db.session.add(sum_task)

            db.session.commit()


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    init_db()
    app.run(debug=True)